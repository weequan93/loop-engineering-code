"""Private bounded HTTP evaluator worker. No authority keys enter this process."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop_engineering.execution_tools import http_load
from loop_engineering.workspace import atomic_write
from reference.core import canonical_digest


def main():
    raw = sys.stdin.buffer.read(2097153)
    if len(raw) > 2097152:
        raise ValueError("Executor input exceeds 2 MiB")
    job = json.loads(raw)
    report = {"bindings": job["request"], "plan_digest": canonical_digest(job["plan"]),
              **http_load(job["plan"])}
    destination = Path(job["artifacts"])
    atomic_write(destination / "report.json", (json.dumps(report, sort_keys=True) + "\n").encode())
    print(json.dumps({"result": report["result"], "artifacts": ["report.json"]}))


if __name__ == "__main__":
    main()
