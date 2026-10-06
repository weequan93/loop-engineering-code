"""Offline checks for the implementation-first evaluation policy."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.core import strict_json_loads
from loop_engineering.contracts import ContractError, ROOT
from loop_engineering.readiness import assess_readiness, load_readiness
from scripts.check_readiness import main


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.inventory = strict_json_loads((ROOT / "docs/implementation-status.json").read_text())

    def test_repository_readiness_preserves_explicit_dispatch_policy(self):
        result = load_readiness()
        unfinished = [item for item in self.inventory["items"] if any(item[field] != "complete"
                      for field in ("definition", "implementation", "offline_coverage"))]
        self.assertEqual(result["implementation_complete"], not unfinished)
        self.assertEqual(result["live_evaluation_prerequisite"], "deferred" if unfinished else "met")
        self.assertFalse(result["automatic_live_dispatch"])
        self.assertEqual(result["additional_prerequisites"], ["Current full offline validation passes", "Explicit operator dispatch"])

    def test_live_gate_exits_nonzero_while_report_is_read_only(self):
        inventory = deepcopy(self.inventory)
        inventory["items"][0].update(implementation="partial", offline_coverage="partial", remaining_work="Offline fixture still unfinished")
        result = assess_readiness(inventory)
        with patch("scripts.check_readiness.load_readiness", return_value=result), redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--for-live-evaluation"]), 1)

    def test_omitting_or_duplicating_a_requirement_cannot_open_gate(self):
        for mutate in (lambda items: items.pop(), lambda items: items.append(deepcopy(items[0]))):
            inventory = deepcopy(self.inventory)
            mutate(inventory["items"])
            with self.assertRaises(ContractError):
                assess_readiness(inventory)

    def test_unknown_and_boolean_statuses_are_rejected(self):
        for status in ("done", True, None, []):
            inventory = deepcopy(self.inventory)
            inventory["items"][0]["implementation"] = status
            with self.assertRaises(ContractError):
                assess_readiness(inventory)

    def test_live_policy_cannot_be_silently_weakened(self):
        for value in (True, 0, None, "false"):
            inventory = deepcopy(self.inventory)
            inventory["test_policy"]["automatic_live_dispatch"] = value
            with self.assertRaises(ContractError):
                assess_readiness(inventory)

    def test_completed_claim_requires_references_and_finished_definition(self):
        for field, value in (("implementation_refs", []), ("definition", "partial"),
                             ("remaining_work", "still unfinished")):
            inventory = deepcopy(self.inventory)
            inventory["items"][0][field] = value
            with self.assertRaises(ContractError):
                assess_readiness(inventory)

    def test_unfinished_item_requires_concrete_work(self):
        inventory = deepcopy(self.inventory)
        unfinished = inventory["items"][0]
        unfinished.update(implementation="partial", offline_coverage="partial")
        unfinished["remaining_work"] = " "
        with self.assertRaises(ContractError):
            assess_readiness(inventory)

    def test_missing_and_escaping_references_are_rejected(self):
        for ref in ("missing.py", "../README.md", "/etc/hosts"):
            inventory = deepcopy(self.inventory)
            inventory["items"][0]["implementation_refs"] = [ref]
            with self.assertRaises(ContractError):
                assess_readiness(inventory)

    def test_symlink_reference_outside_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside.py").symlink_to(ROOT / "README.md")
            inventory = deepcopy(self.inventory)
            for item in inventory["items"]:
                for refs in ("definition_refs", "implementation_refs", "test_refs"):
                    item[refs] = ["outside.py"]
            with self.assertRaises(ContractError):
                assess_readiness(inventory, root=root)

    def test_completed_fixture_only_satisfies_inventory_prerequisite(self):
        inventory = deepcopy(self.inventory)
        for item in inventory["items"]:
            item.update(definition="complete", implementation="complete", offline_coverage="complete",
                        definition_refs=["docs/architecture.md"], implementation_refs=["loop_engineering/controller.py"],
                        test_refs=["tests/test_readiness.py"], remaining_work=None)
        result = assess_readiness(inventory)
        self.assertTrue(result["implementation_complete"])
        self.assertEqual(result["live_evaluation_prerequisite"], "met")
        self.assertFalse(result["automatic_live_dispatch"])
        self.assertEqual(len(result["additional_prerequisites"]), 2)
