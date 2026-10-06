"""Explicit host activity and version handshake; polling cannot invent work."""
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT
from loop_engineering.host_install import install
from loop_engineering.native_host import render_progress
from tests import test_native_host as fixture


class NativeSessionTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start

    def test_heartbeat_expires_without_polling_renewal_and_preserves_child_budget(self):
        self.start()
        self.service.clock = lambda: 1000
        before = self.service._data(self.team_id)
        self.assertEqual(self.service.progress(self.team_id)["host_activity"]["state"], "unknown")
        self.service.heartbeat(self.team_id, "actual-host", "Inspecting <draft>", ttl_seconds=30)
        snapshot = self.service._data(self.team_id)
        self.assertEqual(snapshot["records"], before["records"])
        self.service.clock = lambda: 1031
        result = self.service.progress(self.team_id)
        self.assertEqual(result["host_activity"]["state"], "stale_report")
        self.assertEqual(self.service._data(self.team_id), snapshot)
        self.assertIn("Inspecting &lt;draft&gt;", render_progress(result))
        self.assertEqual(result["host_activity"]["host_usage"], "unknown")
        self.assertEqual(result["host_activity"]["recorded_controller_seconds"], 0)

    def test_competing_host_unknown_task_and_stop_do_not_claim_activity(self):
        self.start(); self.service.clock = lambda: 1000
        self.service.heartbeat(self.team_id, "one", "Actual planning")
        for kwargs in ({"owner": "two"}, {"task_id": "absent"}, {"ttl_seconds": True}):
            with self.assertRaises(ContractError):
                self.service.heartbeat(**{**dict(team_id=self.team_id, owner="one", activity="Actual planning"), **kwargs})
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "Stopped"):
            self.service.heartbeat(self.team_id, "one", "Must not resume")
        self.assertEqual(self.service.progress(self.team_id)["host_activity"]["state"], "stopped")

    def test_published_upgrade_customization_and_restart_are_distinct(self):
        install(self.root, self.store.directory)
        report = self.service.handshake()
        self.assertFalse(report["restart_required"])
        self.assertEqual(report["installed_instructions"][0]["status"], "current")
        path = self.root / ".agents/skills/loop-engineering/SKILL.md"
        path.write_bytes((ROOT / "tests/fixtures/native-host-v6-skill.md").read_bytes())
        self.assertEqual(self.service.handshake()["installed_instructions"][0]["status"], "upgrade_available")
        path.write_text("My custom instructions")
        self.assertEqual(self.service.handshake()["installed_instructions"][0]["status"], "customized")
        before = path.read_bytes()
        self.start()
        with patch("loop_engineering.native_session.fingerprint", return_value="changed"):
            self.assertTrue(self.service.handshake()["restart_required"])
            self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "reconnect_framework")
            with self.assertRaisesRegex(ContractError, "reconnect"):
                self.service.task_request(self.team_id, "implement")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.store.runs())
