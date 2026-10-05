"""SYNTHETIC evidence for examples/tests. This is not an evidence collector.

These fixtures deliberately construct the trusted caller inputs in memory.
Production inputs must be authenticated externally as explained in GateContext.
"""

from pathlib import Path
from typing import Any

from .core import GateContext, canonical_digest, strict_json_loads


ROOT = Path(__file__).resolve().parents[1]


def load_example(path: str) -> dict[str, Any]:
    return strict_json_loads((ROOT / path).read_text(encoding="utf-8"))


def make_record(task: dict[str, Any], check: dict[str, Any],
                snapshot: str, environment: str) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "evidence_id": "fixture-" + check["id"],
        "task_id": task["task_id"],
        "contract_digest": canonical_digest(task),
        "snapshot_digest": snapshot,
        "environment_digest": environment,
        "check_id": check["id"],
        "check_digest": canonical_digest(check),
        "executor_run_id": "synthetic-run",
        "started_at": "2026-10-02T05:00:00Z",
        "finished_at": "2026-10-02T05:00:01Z",
        "result": "pass",
        "exit_code": 0 if check["type"] == "command" else None,
        "artifacts": [{"uri": "memory://synthetic/" + check["id"] + ".txt",
                       "sha256": canonical_digest({"synthetic_log": check["id"]})}],
        "summary": "Synthetic passing result. No check was actually executed.",
        "extensions": {"synthetic_fixture": True},
    }


def fixture_context(task: dict[str, Any], records: list[dict[str, Any]], *,
                    snapshot: str | None = None, environment: str | None = None,
                    independent: bool = False) -> GateContext:
    digests = frozenset(canonical_digest(record) for record in records)
    return GateContext(
        approved_contract_digest=canonical_digest(task),
        snapshot_digest=snapshot or canonical_digest({"synthetic_candidate": 1}),
        environment_digest=environment or canonical_digest({"synthetic_environment": 1}),
        verified_record_digests=digests,
        independent_record_digests=digests if independent else frozenset(),
        policy_clear=True,
        effects_reconciled=True,
    )


def make_fixture() -> tuple[dict[str, Any], list[dict[str, Any]], GateContext]:
    task = load_example("templates/task.json")
    snapshot = canonical_digest({"synthetic_candidate": 1})
    environment = canonical_digest({"synthetic_environment": 1})
    records = [make_record(task, check, snapshot, environment) for check in task["checks"]]
    return task, records, fixture_context(task, records)
