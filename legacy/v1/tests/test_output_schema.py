"""Offline regression for the schema rejection observed from an actual CLI.

The gateway fixture checks transport restrictions, not live model conformance.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator

from loop_engineering.adapters import CodexDriver
from loop_engineering.contracts import ROOT, ContractError, load, validate
from loop_engineering.models import ModelDriver
from loop_engineering.output_schema import structured_output_schema
from loop_engineering.team_automation import PLAN_RESPONSE, RECEIPT_RESPONSE, REVIEW_RESPONSE, SPEC_RESPONSE
from loop_engineering.team_host import TeamCodexDriver


def gateway_accepts(schema):
    """A deterministic strict-gateway fixture, including the observed error."""
    if "type" not in schema and not ({"anyOf", "$ref"} & schema.keys()):
        raise AssertionError("schema must have a type key")
    if {"const", "uniqueItems", "minLength", "maxLength"} & schema.keys():
        raise AssertionError("Unsupported generation constraint")
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            raise AssertionError("Objects must be closed")
        if set(schema.get("required", [])) != set(schema.get("properties", {})):
            raise AssertionError("All object fields must be required")
        for child in schema["properties"].values():
            gateway_accepts(child)
    if "items" in schema:
        gateway_accepts(schema["items"])
    for child in schema.get("anyOf", []):
        gateway_accepts(child)


class OutputSchemaTests(unittest.TestCase):
    def test_codex_worker_schema_passes_gateway_and_preserves_contract_file(self):
        path = ROOT / "schemas/step-v0.2.schema.json"
        original = path.read_bytes()
        with self.assertRaisesRegex(AssertionError, "type key"):
            gateway_accepts(json.loads(original))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            CodexDriver(executable=sys.executable).command(root, root)
            wire = load(root / "step.schema.json")
            gateway_accepts(wire)
            Draft202012Validator(wire).validate(load(ROOT / "examples/step-v0.2.json"))
        self.assertEqual(path.read_bytes(), original)

    def test_every_team_operation_uses_a_gateway_compatible_schema(self):
        schemas = [PLAN_RESPONSE, RECEIPT_RESPONSE, REVIEW_RESPONSE, SPEC_RESPONSE,
                   load(ROOT / "schemas/step-v0.2.schema.json")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for schema in schemas:
                with self.subTest(schema=schema.get("title", list(schema["properties"]))):
                    saved = deepcopy(schema)
                    TeamCodexDriver(schema, executable=sys.executable).command(root, root)
                    gateway_accepts(load(root / "step.schema.json"))
                    self.assertEqual(schema, saved)

    def test_provider_generation_schema_keeps_local_uniqueness_enforcement(self):
        engine = load(ROOT / "templates/engine.json")
        driver = ModelDriver(engine["model"], engine["response_limits"], None)
        original = load(ROOT / "schemas/step-v0.2.schema.json")
        payload = driver.prepare_json({"fixture": True}, original, "Return an AgentStep")
        wire = payload["text"]["format"]["schema"]
        gateway_accepts(wire)
        response = load(ROOT / "examples/step-v0.2.json")
        response["criterion_ids"] = ["same", "same"]
        Draft202012Validator(wire).validate(response)
        with self.assertRaises(ContractError):
            validate("step", response)
        self.assertTrue(original["properties"]["criterion_ids"]["uniqueItems"])

    def test_unsupported_optional_and_composition_schemas_refuse_preparation(self):
        engine = load(ROOT / "templates/engine.json")
        driver = ModelDriver(engine["model"], engine["response_limits"], None)
        optional = {"type": "object", "additionalProperties": False,
                    "properties": {"maybe": {"type": "string"}}, "required": []}
        with self.assertRaisesRegex(ContractError, "every field required"):
            driver.prepare_json({}, optional, "Return JSON")
        composed = deepcopy(RECEIPT_RESPONSE)
        composed["allOf"] = [{"type": "object"}]
        with self.assertRaisesRegex(ContractError, "Unsupported"):
            structured_output_schema(composed)


if __name__ == "__main__":
    unittest.main()
