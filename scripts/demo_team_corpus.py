"""Explicit offline collaboration corpus. No live model or network dispatch."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reference.team_corpus import run_notes_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"schema_version": "1.0", "recorded_at": datetime.now(timezone.utc).isoformat(),
              "live_model_calls": 0, "cases": [run_notes_case(), run_notes_case(inject_regression=True)]}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if all(item["verified_success"] for item in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
