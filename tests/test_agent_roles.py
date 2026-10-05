"""Specialist/covered role contexts and actual offline scheduler integration."""

import json
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from loop_engineering.project import initialize
from loop_engineering.scenarios import MAX_INPUT_BYTES, context, task_inputs_digest
from tests import test_scenarios as scenario_fixture
from tests import test_team_automation as automation_fixture


PROTOCOL = ".loop/agent-protocol.md"


def instruction_paths(material):
    return {item["path"] for item in material["instructions"]}


def role_paths(paths):
    return {path for path in paths if path.startswith(".loop/agents/")}


class RoleContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = scenario_fixture.ScenarioTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def test_new_preset_exports_protocol_offline_and_sizes_full_covered_context(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No setup dispatch")):
            self.fixture.initialize()
            self.fixture.select_team()
            bundle = context(self.root)
        self.assertIn(PROTOCOL, {item["path"] for item in bundle["documents"]})
        self.assertEqual(bundle["project"]["context_max_bytes"], 120000)
        self.assertLessEqual(len(json.dumps(bundle, ensure_ascii=False,
                                           separators=(",", ":")).encode()), 120000)
        self.assertEqual(bundle["role_assignment"]["owner"], "coordinator")
        self.assertEqual({role["id"] for role in bundle["role_assignment"]["covered_roles"]},
                         {"requirements_reviewer", "architect", "tester", "integrator"})
        paths = {item["path"] for item in bundle["documents"]}
        self.assertEqual(role_paths(paths), {f".loop/agents/{name}.md" for name in
                         ("coordinator", "requirements_reviewer", "architect", "tester", "integrator")})

    def test_covered_role_export_receives_own_and_owner_contract_only(self):
        self.fixture.initialize()
        self.fixture.select_team()
        bundle = context(self.root, "tester")
        self.assertEqual(bundle["role_assignment"]["owner"], "coordinator")
        self.assertEqual(bundle["role_assignment"]["covered_roles"], [])
        self.assertEqual(role_paths({item["path"] for item in bundle["documents"]}),
                         {".loop/agents/tester.md", ".loop/agents/coordinator.md"})

    def test_other_active_specialist_contracts_are_not_copied(self):
        self.fixture.initialize()
        self.fixture.select_team(all_roles=True)
        for name in scenario_fixture.DEVELOPMENT_ROLES:
            with self.subTest(role=name):
                bundle = context(self.root, name)
                self.assertEqual(bundle["role_assignment"]["owner"], name)
                self.assertEqual(bundle["role_assignment"]["covered_roles"], [])
                self.assertEqual(role_paths({item["path"] for item in bundle["documents"]}),
                                 {f".loop/agents/{name}.md"})
                self.assertIn(PROTOCOL, {item["path"] for item in bundle["documents"]})

    def test_allowed_backend_coverage_loads_actual_frontend_contract(self):
        self.fixture.initialize()
        selected = self.fixture.select_team()
        next(item for item in selected["decisions"] if item["role"] == "frontend").update(
            state="covered", covered_by="backend", handoff_to=["coordinator"])
        self.fixture.write(".loop/team.json", selected)
        bundle = context(self.root, "backend")
        self.assertEqual([role["id"] for role in bundle["role_assignment"]["covered_roles"]], ["frontend"])
        self.assertEqual(role_paths({item["path"] for item in bundle["documents"]}),
                         {".loop/agents/backend.md", ".loop/agents/frontend.md"})

    def test_stale_and_pending_selections_do_not_create_coverage(self):
        self.fixture.initialize()
        selected = self.fixture.select_team()
        spec = self.root / ".loop/spec.md"
        original = spec.read_text()
        spec.write_text(original + "\nA changed requirement.\n")
        bundle = context(self.root)
        self.assertEqual(bundle["status"]["team"]["state"], "stale")
        self.assertEqual(bundle["role_assignment"]["covered_roles"], [])
        self.assertEqual(context(self.root, "tester")["role_assignment"]["owner"], "tester")
        spec.write_text(original)
        next(item for item in selected["decisions"] if item["role"] == "reviewer").update(
            state="pending", assignee=None, handoff_to=[])
        self.fixture.write(".loop/team.json", selected)
        bundle = context(self.root)
        self.assertEqual(bundle["status"]["team"]["state"], "pending")
        self.assertEqual(bundle["role_assignment"]["covered_roles"], [])

    def test_init_preserves_custom_protocol_and_existing_context_limit(self):
        initialize(self.root)
        profile = load(self.root / ".loop/project.json")
        profile["context_max_bytes"] = 76543
        self.fixture.write(".loop/project.json", profile)
        self.fixture.initialize()
        protocol = self.root / PROTOCOL
        protocol.write_text("Project-specific specialist protocol.\n")
        self.fixture.initialize()
        self.assertEqual(protocol.read_text(), "Project-specific specialist protocol.\n")
        self.assertEqual(load(self.root / ".loop/project.json")["context_max_bytes"], 76543)

    def test_legacy_manifest_does_not_require_new_protocol(self):
        self.fixture.initialize()
        manifest = load(self.root / ".loop/scenario.json")
        manifest.pop("agent_protocol")
        manifest.pop("context_max_bytes")
        self.fixture.write(".loop/scenario.json", manifest)
        (self.root / PROTOCOL).unlink()
        bundle = context(self.root, "backend")
        self.assertNotIn(PROTOCOL, {item["path"] for item in bundle["documents"]})

    def test_required_protocol_rejects_missing_symlink_invalid_or_oversize_text(self):
        self.fixture.initialize()
        protocol = self.root / PROTOCOL
        original = protocol.read_bytes()
        for kind in ("missing", "symlink", "nul", "oversize"):
            with self.subTest(kind=kind):
                protocol.unlink()
                if kind == "symlink":
                    protocol.symlink_to(self.fixture.spec)
                elif kind == "nul":
                    protocol.write_bytes(b"invalid\0protocol")
                elif kind == "oversize":
                    protocol.write_bytes(b"x" * (MAX_INPUT_BYTES + 1))
                with self.assertRaises((ContractError, FileNotFoundError)):
                    context(self.root)
                if protocol.exists() or protocol.is_symlink():
                    protocol.unlink()
                protocol.write_bytes(original)

    def test_protocol_is_bound_but_progress_documents_stay_mutable(self):
        self.fixture.initialize()
        before = task_inputs_digest(self.root)
        progress = self.root / ".loop/plan.md"
        progress.write_text(progress.read_text() + "\nCurrent progress.\n")
        self.assertEqual(before, task_inputs_digest(self.root))
        protocol = self.root / PROTOCOL
        protocol.write_text(protocol.read_text() + "\nA revised role rule.\n")
        self.assertNotEqual(before, task_inputs_digest(self.root))


class RoleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automation_fixture.AutomationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def test_actual_workers_and_recipients_receive_effective_contracts(self):
        self.fixture.start()
        result = self.fixture.auto.run(self.fixture.team_id)
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        self.assertTrue(result["final_candidate_current"])
        requests = self.fixture.transport.requests
        workers = [item for item in requests if item["kind"] == "team-host-request"]
        self.assertEqual(len(workers), 2)
        for item in workers:
            paths = instruction_paths(item["team_assignment"])
            self.assertIn(PROTOCOL, paths)
            expected = ({".loop/agents/backend.md"} if item["team_assignment"]["role"] == "backend"
                        else {".loop/agents/tester.md", ".loop/agents/coordinator.md"})
            self.assertEqual(role_paths(paths), expected)
        recipients = [item for item in requests if item["kind"] == "team-recipient-request"]
        self.assertEqual(len(recipients), 2)
        for item in recipients:
            material = item["context"]["scenario"]
            self.assertEqual({role["id"] for role in material["role_assignment"]["covered_roles"]},
                             {"requirements_reviewer", "architect", "tester", "integrator"})
            self.assertIn(PROTOCOL, instruction_paths(material))
            self.assertIn(".loop/agents/tester.md", instruction_paths(material))

    def test_separate_native_reviews_receive_only_acceptance_and_shared_protocol(self):
        self.fixture.native_acceptance()
        self.fixture.start()
        result = self.fixture.auto.run(self.fixture.team_id)
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        reviews = [item for item in self.fixture.transport.requests if item["kind"] == "team-evaluator-request"]
        self.assertEqual(len(reviews), 2)
        self.assertNotEqual(reviews[0]["request"]["context_id"], reviews[1]["request"]["context_id"])
        for item in reviews:
            material = item["inspection"]["scenario"]
            self.assertEqual(material["role_assignment"]["owner"], "acceptance")
            self.assertEqual(material["role_assignment"]["covered_roles"], [])
            self.assertEqual(role_paths(instruction_paths(material)), {".loop/agents/acceptance.md"})
            self.assertIn(PROTOCOL, instruction_paths(material))
        child = self.fixture.team.store.get(result["tasks"]["verify"]["integration_run_id"])
        self.assertTrue(child["native"]["external_origins"])

    def test_covered_recipient_receives_owner_contract_and_its_own_duties(self):
        selection = load(self.root / ".loop/team.json")
        next(item for item in selection["decisions"] if item["role"] == "backend")["handoff_to"].append("tester")
        (self.root / ".loop/team.json").write_text(json.dumps(selection))
        self.fixture.plan["tasks"][0]["handoff_to"] = ["tester"]
        self.fixture.fixture.save_plan()
        self.fixture.start()
        result = self.fixture.auto.run(self.fixture.team_id)
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        receipt = next(item for item in self.fixture.transport.requests
                       if item["kind"] == "team-recipient-request" and item["role"] == "tester")
        material = receipt["context"]["scenario"]
        self.assertEqual(material["role_assignment"]["owner"], "coordinator")
        self.assertEqual(role_paths(instruction_paths(material)),
                         {".loop/agents/tester.md", ".loop/agents/coordinator.md"})
        self.assertIn(PROTOCOL, instruction_paths(material))

    def test_changed_protocol_blocks_frozen_team_before_dispatch(self):
        self.fixture.start()
        protocol = self.root / PROTOCOL
        protocol.write_text(protocol.read_text() + "\nChanged procedure.\n")
        with self.assertRaisesRegex(ContractError, "inputs changed"):
            self.fixture.team.request(self.fixture.team_id, "implement", managed=True)
        self.assertEqual(self.fixture.transport.requests, [])
        self.assertEqual(self.fixture.team.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
