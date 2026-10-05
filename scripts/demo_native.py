"""Offline native integration: two synthetic provider protocols, real checks,
held ambiguous spend, staged acceptance, registered review, replay, and restore.

No network transport, model API, agent CLI, or Docker command is invoked.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import platform
import shutil
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop_engineering import __version__
from loop_engineering.budgets import Ledger
from loop_engineering.contracts import load
from loop_engineering.evaluators import sign_result
from loop_engineering.models import ModelError
from loop_engineering.native_contracts import validate_context
from loop_engineering.native_engine import NativeController
from loop_engineering.project import initialize
from loop_engineering.store import Store, utc_now
from loop_engineering.workspace import atomic_write

FIXED = 'def slug(text: str) -> str:\n    if not text.strip():\n        return ""\n    return "-".join(text.lower().split(" "))\n'


class ScriptedTransport:
    """Synthetic responses and fictional usage; deliberately never HTTP."""
    def __init__(self, provider: str, *, ambiguous=False):
        self.provider, self.ambiguous, self.operations = provider, ambiguous, []

    def post(self, operation, payload, **limits):
        self.operations.append(operation)
        if operation == "count":
            return {"input_tokens": 100}
        if self.ambiguous:
            raise ModelError("transient", "Synthetic interruption after persisted reservation")
        context = json.loads(payload["input"] if self.provider == "openai" else payload["messages"][0]["content"])
        validate_context(context)
        bundle = context["bundle"]
        source = next(s for s in bundle["sources"] if s["path"] == "slug.py")
        step = {"schema_version": "0.2", "step_id": "scripted-native-fix", "task_id": bundle["task"]["task_id"],
                "contract_digest": context["contract_digest"], "base_snapshot_digest": context["base_snapshot_digest"],
                "intent": "act", "criterion_ids": context["active_stage"]["criterion_ids"],
                "summary": "Scripted focused whitespace fix", "expected_observation": "Both real checks pass",
                "changes": [{"path": "slug.py", "expected_sha256": source["sha256"], "new_content": FIXED}],
                "evidence_refs": [], "blocker": None, "next_action": None}
        usage = {"input_tokens": 100, "output_tokens": 20}
        text = json.dumps(step)
        if self.provider == "openai":
            return {"status": "completed", "usage": usage, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        return {"stop_reason": "end_turn", "usage": usage, "content": [{"type": "text", "text": text}]}


def check_results(store, data, field):
    return [{"check_id": r["check_id"], "result": r["result"], "exit_code": r["exit_code"],
             "snapshot_digest": r["snapshot_digest"]} for r in store.evidence(data["run_id"], data[field])]


def demonstrate(base: Path) -> dict:
    project = base / "project"
    shutil.copytree(ROOT / "examples/slug-project", project)
    initialize(project, task_file=project / "task.json", native=True)
    task_path, config_path = project / ".loop/task.json", project / ".loop/engine.json"
    task = load(task_path, "task")
    task["workflow"] = "staged"
    for check in task["checks"]:
        check["argv"][0] = sys.executable
    task["checks"].append({"id": "independent-review", "type": "review", "independent": True,
         "description": "Inspect the compatibility-preserving candidate",
         "procedure": ["Inspect slug.py and both command results on the requested candidate.",
                       "Record any blocking findings against the task criteria."]})
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    store = Store(base / "state")
    store.authorities.create("offline-reviewer", "reviewer")
    config = load(config_path)
    config["model"].update(model="synthetic-protocol-fixture", pricing={"price_id": "fictional-demo-only", "currency": "USD",
                          "input_microunits_per_million": 1000000, "output_microunits_per_million": 2000000})
    config["response_limits"]["max_output_tokens"] = 128
    config["evaluator_keys"] = [{"key_id": "offline-reviewer", "role": "reviewer", "check_ids": ["independent-review"]}]
    config["stages"] = [{"id": "fix", "depends_on": [], "criterion_ids": ["empty-whitespace"], "write_allow": ["slug.py"], "write_deny": []},
                        {"id": "compatibility", "depends_on": ["fix"], "criterion_ids": ["preserve-behavior"], "write_allow": ["slug.py"], "write_deny": []}]
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    engine = NativeController(store, config_path)
    data = engine.start(project, task_path, project / ".loop/project.json")
    run_id, baseline = data["run_id"], check_results(store, data, "baseline")
    context = engine.context(run_id)
    first = ScriptedTransport("openai", ambiguous=True)
    data = engine.drive(run_id, transport=first)
    assert data["native"]["reservations"][0]["status"] == "unknown"
    before_restart = {"iteration": data["state"]["iteration"], "budget": Ledger(data["native"]["reservations"]).balance()}
    engine = NativeController(Store(base / "state"), config_path)
    data = engine.resume(run_id)
    assert data["state"]["iteration"] == before_restart["iteration"]
    assert Ledger(data["native"]["reservations"]).balance() == before_restart["budget"]
    model = deepcopy(config["model"])
    model.update(provider="anthropic", model="synthetic-second-protocol", api_key_env="ANTHROPIC_API_KEY")
    engine.switch_model(run_id, model)
    second = ScriptedTransport("anthropic")
    data = engine.drive(run_id, transport=second)
    assert data["state"]["status"] == "REVIEWING"
    assert all(stage["status"] == "passed" for stage in data["native"]["stage_status"])
    waiting_review = {"status": data["state"]["status"], "checks": check_results(store, data, "selected_evidence")}
    request = engine.evaluation_request(run_id, "independent-review")
    review_artifact = base / "scripted-review.txt"
    review_artifact.write_text("Synthetic reviewer fixture: focused guard preserves split(' ') behavior. No blocking findings.\n")
    signed = sign_result(store.authorities, request, "offline-reviewer", result="pass",
                         summary="Synthetic reviewer fixture; no live judgment was performed", artifacts=[review_artifact], findings=[])
    engine.import_evaluation(run_id, signed)
    engine.resume(run_id)
    data = engine.verify(run_id)
    assert data["state"]["status"] == "SUCCEEDED"
    assert engine.store.replay(run_id) == engine.store.get(run_id)
    delivered = base / "delivered"
    engine.snapshots.materialize(data["checkpoint_snapshot"], delivered)
    assert engine.snapshots.capture(delivered, data["profile"])["digest"] == data["state"]["snapshot_digest"]
    assert [r["result"] for r in baseline] == ["fail", "pass"]
    # Retained demos include concrete portable contracts without exporting keys.
    atomic_write(base / "context.json", (json.dumps(context, indent=2) + "\n").encode())
    atomic_write(base / "evaluation-request.json", (json.dumps(request, indent=2) + "\n").encode())
    return {"schema_version": "1.0", "framework_version": __version__, "recorded_at": utc_now(),
            "evaluation_kind": "offline-native-integration", "network_requests": 0, "live_model_calls": 0,
            "python": platform.python_version(), "platform": platform.platform(),
            "fixtures": {"provider_responses": "synthetic OpenAI/Anthropic protocol objects", "usage": "fictional counters",
                         "pricing": "fictional USD microcurrency rates", "review": "synthetic registered evaluator judgment"},
            "baseline": baseline, "ambiguous_response": before_restart, "awaiting_review": waiting_review,
            "provider_operations": {"openai_fixture": first.operations, "anthropic_fixture": second.operations},
            "final": {"status": data["state"]["status"], "iterations": data["state"]["iteration"],
                      "checks": check_results(engine.store, data, "selected_evidence"), "stages": data["native"]["stage_status"],
                      "usage": data["state"]["usage"], "budget": Ledger(data["native"]["reservations"]).balance()},
            "resume_preserved_iteration_and_reservations": True, "authenticated_replay_matches": True,
            "restored_snapshot_matches": True, "effective_autonomy": data["state"]["extensions"]["effective_autonomy"],
            "limitations": ["One small Python task; no model-performance measurement.",
                            "Local supervised checks; no deployed Docker boundary was exercised.",
                            "Authentication is exercised, but keys and controller share the host OS administrator.",
                            "An ambiguous fixture retains unknown actual usage; known subtotals are not total spend."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Fresh directory for project, state, context, review, and restoration")
    parser.add_argument("--report", type=Path, help="Write the actual offline integration summary")
    args = parser.parse_args()
    if args.keep:
        if args.keep.exists():
            parser.error("--keep must name a fresh directory")
        args.keep.mkdir(parents=True)
        report = demonstrate(args.keep.resolve())
    else:
        with TemporaryDirectory(prefix="loop-native-demo-") as directory:
            report = demonstrate(Path(directory))
    if args.report:
        atomic_write(args.report, (json.dumps(report, indent=2) + "\n").encode())
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
