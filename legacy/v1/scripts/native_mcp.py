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
    parser.add_argument("--workbench-parent", type=Path, help="Private state subdirectory for supervised host drafts")
    args = parser.parse_args()
    service = None
    try:
        service = NativeHostService(args.project, Store(args.state_dir))
        if args.workbench_parent:
            from loop_engineering.contracts import ContractError
            parent = args.workbench_parent.resolve()
            if not parent.is_relative_to(service.team.store.directory) or parent == service.team.store.directory:
                raise ContractError("Supervised draft parent must be a private state subdirectory")
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            service.workbench_parent = parent
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
