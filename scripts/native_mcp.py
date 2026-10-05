"""Project-scoped coordination; registered review runs need explicit tool calls."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loop_engineering.native_host import NativeHostService
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    service = None
    try:
        service = NativeHostService(args.project, Store(args.state_dir))
        NativeMCPServer(service).serve(sys.stdin.buffer, sys.stdout)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
