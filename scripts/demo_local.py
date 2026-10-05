"""A reproducible file-bridge demo using real source files and real checks.

The proposals are deliberately scripted. This evaluates controller behavior,
not a model's ability to solve the task. Use --keep to inspect the full journal.
"""

import argparse
import json
from pathlib import Path
import platform
import shutil
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop_engineering.contracts import load
from loop_engineering import __version__
from loop_engineering.controller import Controller
from loop_engineering.project import initialize
from loop_engineering.store import Store, utc_now
from loop_engineering.workspace import atomic_write, byte_digest


def proposal(engine, run_id, content, step_id):
    context = engine.context(run_id)
    source = next(item for item in context["sources"] if item["path"] == "slug.py")
    return {"schema_version": "0.2", "step_id": step_id, "task_id": context["task"]["task_id"],
            "contract_digest": context["contract_digest"], "base_snapshot_digest": context["base_snapshot_digest"],
            "intent": "act", "criterion_ids": [criterion["id"] for criterion in context["task"]["criteria"]],
            "summary": "Demonstration file proposal", "expected_observation": "Required behavior and compatibility checks pass",
            "changes": [{"path": "slug.py", "expected_sha256": source["sha256"], "new_content": content}],
            "evidence_refs": [], "blocker": None, "next_action": None}


def checks(engine, data, field):
    return [{"check_id": record["check_id"], "result": record["result"], "exit_code": record["exit_code"],
             "snapshot_digest": record["snapshot_digest"]}
            for record in engine.store.evidence(data["run_id"], data[field])]


def demonstrate(base):
    project = base / "project"
    shutil.copytree(ROOT / "examples/slug-project", project)
    initialize(project, task_file=project / "task.json")
    task_path = project / ".loop/task.json"
    task = load(task_path, "task")
    for check in task["checks"]:
        check["argv"][0] = sys.executable
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    engine = Controller(Store(base / "state"))
    data = engine.start(project, task_path, project / ".loop/project.json")
    baseline = checks(engine, data, "baseline")
    wrong = 'def slug(text: str) -> str:\n    return "-".join(text.lower().split())\n'
    data = engine.submit(data["run_id"], proposal(engine, data["run_id"], wrong, "overbroad-fix"))
    rejected = {"status": data["state"]["status"], "gate": data["last_gate"],
                "checks": checks(engine, data, "selected_evidence")}
    before_resume = data["state"]["iteration"]
    # Fresh controller object with no preceding conversation or in-memory state.
    engine = Controller(Store(base / "state"))
    data = engine.resume(data["run_id"])
    assert data["state"]["iteration"] == before_resume
    correct = 'def slug(text: str) -> str:\n    if not text.strip():\n        return ""\n    return "-".join(text.lower().split(" "))\n'
    data = engine.submit(data["run_id"], proposal(engine, data["run_id"], correct, "bounded-fix"))
    assert data["state"]["status"] == "SUCCEEDED", data["state"]["reason"]
    delivered = base / "delivered"
    engine.snapshots.materialize(data["checkpoint_snapshot"], delivered)
    restored = engine.snapshots.capture(delivered, data["profile"])["digest"]
    assert restored == data["state"]["snapshot_digest"]
    assert [item["result"] for item in baseline] == ["fail", "pass"]
    assert rejected["status"] != "SUCCEEDED"
    return {"schema_version": "0.2", "framework_version": __version__, "recorded_at": utc_now(),
            "evaluation_kind": "local-controller-integration", "backend": "scripted-file-proposals",
            "model": None, "python": platform.python_version(), "platform": platform.platform(),
            "baseline": baseline, "overbroad_fix": rejected,
            "final": {"status": data["state"]["status"], "checks": checks(engine, data, "selected_evidence"),
                      "iterations": data["state"]["iteration"], "wall_seconds": data["state"]["usage"]["wall_seconds"],
                      "tokens": data["state"]["usage"]["tokens"], "cost_microunits": None,
                      "delivered_source_digest": byte_digest((delivered / "slug.py").read_bytes())},
            "resume_preserved_iteration_count": True, "restored_snapshot_matches": True,
            "event_count": len(engine.store.events(data["run_id"])),
            "limitations": ["Proposals are scripted; this is not a model benchmark.",
                            "Records are authenticated, but arbitrary local subprocesses share host OS authority.",
                            "The example is one small Python bug, not a representative task corpus."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Fresh output directory for project, state, artifacts, and restored checkpoint")
    parser.add_argument("--report", type=Path, help="Write the actual portable evaluation summary to this file")
    args = parser.parse_args()
    if args.keep:
        if args.keep.exists():
            parser.error("--keep must name a fresh directory")
        args.keep.mkdir(parents=True)
        report = demonstrate(args.keep.resolve())
    else:
        with TemporaryDirectory(prefix="loop-demo-") as directory:
            report = demonstrate(Path(directory))
    if args.report:
        atomic_write(args.report, (json.dumps(report, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
