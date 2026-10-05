"""Report unfinished implementation; never dispatch an agent or model."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop_engineering.readiness import load_readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--for-live-evaluation", action="store_true",
                        help="Exit 1 if the implementation prerequisite for live evaluation is unmet")
    args = parser.parse_args(argv)
    try:
        result = load_readiness()
        print(json.dumps(result, indent=2))
        return 1 if args.for_live_evaluation and not result["implementation_complete"] else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
