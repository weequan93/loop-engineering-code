"""Run pure decisions on synthetic fixtures. No model or tool execution."""

from dataclasses import replace

from .core import Usage, assess_completion, canonical_digest, negotiate_mode, stop_decision
from .fixtures import load_example, make_fixture


def main() -> None:
    task, records, context = make_fixture()
    print("SYNTHETIC DECISION DEMO: no real agent, checks, or runtime controls.")
    print("Current authenticated fixture:", assess_completion(task, records, context).outcome)
    changed = replace(context, snapshot_digest=canonical_digest({"synthetic_candidate": 2}))
    print("Code changed after checks:", assess_completion(task, records, changed).outcome)
    untrusted = replace(context, verified_record_digests=frozenset())
    print("Unauthenticated evidence:", assess_completion(task, records, untrusted).outcome)
    adapter = load_example("examples/native-adapter.json")
    runtime = load_example("examples/native-runtime.json")
    runtime["capabilities"]["isolated_workspace"] = False
    mode = negotiate_mode("unattended", adapter, runtime)
    print("Missing isolation:", mode.mode, "—", "; ".join(mode.reasons))
    stopped = stop_decision(task["limits"], Usage(iterations=12, wall_seconds=30))
    print("Iteration budget reached:", stopped.outcome if stopped else "continue")


if __name__ == "__main__":
    main()
