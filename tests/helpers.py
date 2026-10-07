import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT = ROOT / "tests" / "fixtures" / "fake_agent.py"

from loop_engineering.store import Project  # noqa: E402


class ProjectCase(unittest.TestCase):
    """A throwaway git project with a failing test and a scripted 'fake' adapter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="loop-test-")
        base = Path(self._tmp.name)
        self.root = base / "project"
        self.fake_state = base / "fake"
        self.root.mkdir()
        self.fake_state.mkdir()
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.root / "test_calc.py").write_text("from calc import add\nassert add(2, 3) == 5\nprint('ok')\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                       cwd=self.root, check=True)
        self.project = Project(self.root)
        self.project.ensure()
        (self.root / ".loop" / "settings.json").write_text(json.dumps({"propose_next": False}))
        (self.root / ".loop" / "adapters.json").write_text(json.dumps({
            "fake": {"argv": [sys.executable, str(FAKE_AGENT), "{mcp_config}"]}}))
        self._env = dict(os.environ)
        os.environ["FAKE_STATE"] = str(self.fake_state)
        os.environ.pop("LOOP_AUTONOMOUS", None)
        os.environ["LOOP_NOTIFY"] = "0"  # no desktop popups from tests
        # Never read the developer's real ~/.config/loop (it may hold a Telegram bot).
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        os.environ.pop("LOOP_TELEGRAM_BOT_TOKEN", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def goal(self, **overrides):
        values = {"kind": "develop", "title": "Fix add", "objective": "add() must add numbers",
                  "acceptance": [{"id": "tests", "run": "python3 test_calc.py"}],
                  "agent": {"adapter": "fake"}, "policy": {"max_iterations": 10, "stagnation_limit": 2}}
        values.update(overrides)
        return self.project.create_goal(values)

    def calls(self):
        log = self.fake_state / "calls.log"
        return log.read_text().split() if log.exists() else []
