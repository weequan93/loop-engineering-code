"""Claude Desktop launches this stdio server; it launches no model."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loop_engineering.desktop_bridge import DesktopService
from loop_engineering.desktop_mcp import MCPServer
from loop_engineering.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        MCPServer(DesktopService(args.project, Store(args.state_dir))).serve(sys.stdin.buffer, sys.stdout)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
