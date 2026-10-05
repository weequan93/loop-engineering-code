"""Deterministic wire fixture; it performs no browser or network work."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from loop_engineering.execution_tools import http_load
from reference.core import canonical_digest


class Response:
    status = 200
    def __init__(self, url): self.url = url
    def geturl(self): return self.url
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return b"fixture"[:limit]


class Opener:
    def open(self, request, timeout): return Response(request.full_url)


def main():
    job = json.load(sys.stdin)
    plan = job["plan"]; destination = Path(job["artifacts"])
    report = {"bindings": job["request"], "plan_digest": canonical_digest(plan)}
    artifacts = ["report.json"]
    if plan["kind"] == "http_load":
        report.update(http_load(plan, opener=Opener()))
    else:
        rows = [{**step, "status": "pass", **({"observed": int(step["value"]) if step["action"] == "assert_count"
                 else step["value"]} if step["action"] in {"assert_count", "assert_text"} else {})} for step in plan["steps"]]
        report.update(result="pass", steps=rows, blocked_requests=[], artifacts=["screenshot.png", "trace.zip"])
        # Wire-fixture bytes, never evidence of a real screenshot or trace.
        (destination / "screenshot.png").write_bytes(b"fixture screenshot bytes")
        (destination / "trace.zip").write_bytes(b"fixture trace bytes")
        artifacts += report["artifacts"]
    if len(sys.argv) > 1 and sys.argv[1] == "wrong_candidate":
        report["bindings"] = {**job["request"], "snapshot_digest": "sha256:" + "0" * 64}
    if len(sys.argv) > 1 and sys.argv[1] == "mutate_source":
        (Path(job["workspace"]) / "slug.py").write_text("mutated by executor")
    if len(sys.argv) > 1 and sys.argv[1] == "escape_artifact":
        artifacts = ["../outside.json"]
    (destination / "report.json").write_text(json.dumps(report))
    print(json.dumps({"result": report["result"], "artifacts": artifacts}))


if __name__ == "__main__":
    main()
