"""Run the repository-local CLI from any current directory."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_engineering.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
