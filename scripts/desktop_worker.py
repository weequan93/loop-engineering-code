"""Owned native Desktop waiter, launched only after team budget admission."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loop_engineering.contracts import load
from loop_engineering.desktop_bridge import MAX_MESSAGE_BYTES, wait_for_response
from loop_engineering.store import Store
from reference.core import strict_json_loads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--schema", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Desktop request exceeds the interchange limit")
        result = wait_for_response(Store(args.state_dir), args.team_id, args.operation_id,
                                   load(args.schema), strict_json_loads(raw.decode()))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
