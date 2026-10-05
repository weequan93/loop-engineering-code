"""Optional full JSON Schema validation of this kit's schemas and examples."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    sys.exit("Optional dependency missing: install 'jsonschema>=4.18,<5' to run schema validation.")

from loop_engineering.contracts import validate, validate_step
from loop_engineering.authority import Authorities
from loop_engineering.broker import Broker
from loop_engineering.budgets import Ledger
from loop_engineering.evaluators import make_request
from loop_engineering.native_contracts import load_engine, validate_context, validate_native
from loop_engineering.stages import validate_stages
from reference.fixtures import load_example, make_fixture


def main() -> None:
    validators = {}
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        schema = load_example(str(path.relative_to(ROOT)))
        Draft202012Validator.check_schema(schema)
        validators[path.name.removesuffix(".schema.json")] = Draft202012Validator(schema, format_checker=FormatChecker())
    files = {
        "docs/implementation-status.json": "readiness",
        "templates/task.json": "task",
        "templates/project.json": "project",
        "templates/adapter.json": "adapter",
        "templates/runtime.json": "runtime",
        "templates/engine.json": "native:engine",
        "templates/scenarios/development/scenario.json": "scenario",
        "templates/scenarios/development/team.json": "team",
        "templates/scenarios/development/workflow-tasks.json": "team-workflow",
        "templates/scenarios/development/team-policy.json": "team-policy",
        "templates/scenarios/development/execution-tools.json": "execution-tools",
        "examples/execution-tools.json": "execution-tools",
        "examples/staged-engine.json": "native:engine",
        "examples/staged-task.json": "task",
        "examples/native-context.json": "native:context",
        "examples/ui-task.json": "task",
        "examples/task-v0.1.json": "task",
        "examples/slug-project/task.json": "task",
        "examples/step-v0.2.json": "step",
        "examples/step-v0.3.json": "step",
        "examples/codex-adapter.json": "adapter",
        "examples/command-adapter.json": "adapter",
        "examples/local-runtime.json": "runtime",
        "examples/native-adapter.json": "adapter",
        "examples/native-runtime.json": "runtime",
        "examples/evidence.json": "evidence",
        "examples/state.json": "state",
    }
    for path, kind in files.items():
        data = load_example(path)
        if kind == "native:engine":
            load_engine(ROOT / path)
        elif kind == "native:context":
            validate_context(data)
        else:
            validate(kind, data)
    graph = load_example("examples/staged-engine.json")["stages"]
    validate_stages(load_example("examples/staged-task.json"), graph)
    example_step = load_example("examples/step-v0.2.json")
    validate_step(example_step, load_example("templates/task.json"), example_step["base_snapshot_digest"])
    modern_step = load_example("examples/step-v0.3.json")
    modern_task = load_example("templates/task.json")
    modern_task.setdefault("extensions", {})["agent_step_version"] = "0.3"
    validate_step(modern_step, modern_task, modern_step["base_snapshot_digest"])
    task, records, _ = make_fixture()
    validate("task", task)
    for record in records:
        validators["evidence"].validate(record)
    context = load_example("examples/native-context.json")
    data = {"task": context["bundle"]["task"], "state": context["bundle"]["state"], "workspace": str(ROOT / "examples/slug-project")}
    broker = Broker()
    action = broker.proposal(data, "workspace.read", {"path": "slug.py"})
    validate_native("action", action)
    authorized = broker.authorize(data, action, actor_id="implementer")
    validate_native("authorized_action", authorized)
    validate_native("action_result", {"schema_version": "1.0", "action_id": authorized["action_id"], "outcome": "succeeded",
                                     "actual_effects": {"synthetic_fixture": True}, "artifact_refs": [], "known_usage": {}})
    request = make_request(data, data["task"]["checks"][-1], context["base_snapshot_digest"], data["state"]["environment_digest"])
    validate_native("evaluator_request", request)
    ledger = Ledger([])
    ledger.reserve("synthetic-request", {"input_tokens": 100, "max_output_tokens": 128, "bound_verified": False,
                   "pricing": None, "request_digest": context["contract_digest"]}, data["task"]["limits"])
    ledger.settle("synthetic-request", None)
    validate_native("reservation", ledger.reservations[0])
    validate_native("budget_balance", ledger.balance())
    with TemporaryDirectory(prefix="loop-schema-authority-") as directory:
        authorities = Authorities(Path(directory))
        authorities.create("synthetic-controller", "controller")
        envelope = authorities.sign("synthetic-controller", "evaluation_request", request)
        validate_native("attestation", envelope)
        assert authorities.verify(envelope, "evaluation_request") == request
    print(f"Validated {len(validators)} schema files, {len(files)} saved examples/templates, {len(records)} evidence fixtures, and native action/context/reservation/attestation fixtures.")


if __name__ == "__main__":
    main()
