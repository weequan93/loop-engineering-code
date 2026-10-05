"""One-time host setup never changes unrelated preferences or launches models."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from loop_engineering.cli import main
from loop_engineering.contracts import ContractError
from loop_engineering.host_install import configuration, install
from loop_engineering.workspace import atomic_write


class HostInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project with spaces"; self.root.mkdir()
        self.state = self.base / "private-state"

    def test_codex_project_skill_and_config_install_once_preserving_existing_settings(self):
        (self.root / ".codex").mkdir()
        target = self.root / ".codex/config.toml"
        target.write_text('model = "existing-user-choice"\n[mcp_servers.other]\ncommand = "other-server"\n')
        with patch("subprocess.Popen", side_effect=AssertionError("Installation is offline")):
            result = install(self.root, self.state)
            same = install(self.root, self.state)
        parsed = tomllib.loads(target.read_text())
        self.assertEqual(parsed["model"], "existing-user-choice")
        self.assertEqual(parsed["mcp_servers"]["other"]["command"], "other-server")
        self.assertEqual(parsed["mcp_servers"]["loop-native"]["args"][-1], str(self.state))
        self.assertTrue((self.root / ".agents/skills/loop-engineering/SKILL.md").is_file())
        self.assertEqual(same["files_written"], 0)
        self.assertFalse(result["automatic_dispatch"])
        self.assertFalse(result["global_settings_modified"])
        self.assertFalse(result["native_app_connection_verified"])
        self.assertFalse((self.root / ".loop").exists())

    def test_claude_code_skill_and_json_registry_preserve_other_servers(self):
        target = self.root / ".mcp.json"
        target.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "existing": True}))
        install(self.root, self.state, "claude-code")
        parsed = json.loads(target.read_text())
        self.assertTrue(parsed["existing"])
        self.assertEqual(parsed["mcpServers"]["other"], {"command": "other"})
        self.assertEqual(parsed["mcpServers"]["loop-native"]["type"], "stdio")
        self.assertTrue((self.root / ".claude/skills/loop-engineering/SKILL.md").is_file())
        self.assertFalse((self.root / ".codex").exists())

    def test_desktop_exports_only_scoped_configuration_and_project_instructions(self):
        result = install(self.root, self.state, "claude-desktop")
        self.assertFalse(result["host_settings_modified"])
        config = json.loads((self.root / ".loop/claude-desktop-native-mcp.json").read_text())
        self.assertIn("native_mcp.py", config["mcpServers"]["loop-native"]["args"][0])
        self.assertTrue((self.root / ".loop/native-host-instructions.md").is_file())
        self.assertFalse((self.root / ".agents").exists())

    def test_config_preview_has_no_project_or_state_writes(self):
        result = configuration(self.root, self.state)
        parsed = tomllib.loads(result["configuration_text"])
        self.assertEqual(parsed["mcp_servers"]["loop-native"], result["server_entry"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(self.state.exists())

    def test_exact_previous_published_skill_is_upgraded_once_and_other_settings_preserved(self):
        from loop_engineering.contracts import ROOT
        old = (ROOT / "tests/fixtures/native-host-v1-skill.md").read_bytes()
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        atomic_write(target, old)
        registry = self.root / ".codex/config.toml"
        atomic_write(registry, b'model = "preserved"\n')
        result = install(self.root, self.state)
        self.assertEqual(result["files_upgraded"], [str(target)])
        self.assertIn(b"loop_next", target.read_bytes())
        self.assertEqual(tomllib.loads(registry.read_text())["model"], "preserved")
        self.assertEqual(install(self.root, self.state)["files_written"], 0)

    def test_previous_continuous_flow_skill_upgrades_to_workbench_without_settings_changes(self):
        from loop_engineering.contracts import ROOT
        install(self.root, self.state)
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        registry = self.root / ".codex/config.toml"
        before = registry.read_bytes()
        atomic_write(target, (ROOT / "tests/fixtures/native-host-v2-skill.md").read_bytes())
        result = install(self.root, self.state)
        self.assertEqual(result["files_written"], 1)
        self.assertEqual(registry.read_bytes(), before)
        self.assertIn(b"loop_workbench_prepare", target.read_bytes())

    def test_edited_previous_skill_is_preserved_and_exact_desktop_export_can_upgrade(self):
        from loop_engineering.contracts import ROOT
        old = (ROOT / "tests/fixtures/native-host-v1-skill.md").read_bytes()
        target = self.root / ".loop/native-host-instructions.md"
        atomic_write(target, old + b"\nActual user's local preference\n")
        with self.assertRaisesRegex(ContractError, "customized host file"):
            install(self.root, self.state, "claude-desktop")
        self.assertEqual(target.read_bytes(), old + b"\nActual user's local preference\n")
        self.assertFalse((self.root / ".loop/claude-desktop-native-mcp.json").exists())
        target.write_bytes(old)
        result = install(self.root, self.state, "claude-desktop")
        self.assertEqual(result["files_upgraded"], [str(target)])

    def test_exact_workbench_skill_upgrades_to_specialist_coordination_preserving_settings(self):
        from loop_engineering.contracts import ROOT
        install(self.root, self.state)
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        registry = self.root / ".codex/config.toml"
        before = registry.read_bytes()
        atomic_write(target, (ROOT / "tests/fixtures/native-host-v3-skill.md").read_bytes())
        result = install(self.root, self.state)
        self.assertEqual(result["files_written"], 1)
        self.assertEqual(result["files_upgraded"], [str(target)])
        self.assertEqual(registry.read_bytes(), before)
        self.assertIn(b"loop_worker_assign", target.read_bytes())
        self.assertIn(b"loop_dispatch_board", target.read_bytes())
        self.assertEqual(install(self.root, self.state)["files_written"], 0)

    def test_exact_previous_skill_upgrades_for_background_continuation(self):
        from loop_engineering.contracts import ROOT
        install(self.root, self.state)
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        registry = self.root / ".codex/config.toml"
        before = registry.read_bytes()
        atomic_write(target, (ROOT / "tests/fixtures/native-host-v5-skill.md").read_bytes())
        result = install(self.root, self.state)
        self.assertEqual(result["files_written"], 1)
        self.assertEqual(result["files_upgraded"], [str(target)])
        self.assertEqual(registry.read_bytes(), before)
        self.assertIn(b"loop_operation_status", target.read_bytes())
        self.assertIn(b"loop_plan_preflight", target.read_bytes())
        self.assertEqual(install(self.root, self.state)["files_written"], 0)

    def test_exact_continuation_skill_upgrades_without_changing_project_configuration(self):
        from loop_engineering.contracts import ROOT
        install(self.root, self.state)
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        registry = self.root / ".codex/config.toml"
        before = registry.read_bytes()
        for version in (6, 7):
            atomic_write(target, (ROOT / f"tests/fixtures/native-host-v{version}-skill.md").read_bytes())
            result = install(self.root, self.state)
            self.assertEqual(result["files_upgraded"], [str(target)])
            self.assertEqual(target.read_bytes(), (ROOT / "templates/native-host/loop-engineering/SKILL.md").read_bytes())
            self.assertEqual(registry.read_bytes(), before)
            self.assertEqual(install(self.root, self.state)["files_written"], 0)

    def test_customized_skill_or_different_server_is_preserved_before_any_write(self):
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        atomic_write(target, b"Actual customized skill\n")
        with self.assertRaisesRegex(ContractError, "customized host file"):
            install(self.root, self.state)
        self.assertEqual(target.read_text(), "Actual customized skill\n")
        self.assertFalse((self.root / ".codex").exists())
        target.unlink()
        atomic_write(self.root / ".codex/config.toml", b'[mcp_servers.loop-native]\ncommand="different"\nargs=[]\n')
        with self.assertRaisesRegex(ContractError, "different settings"):
            install(self.root, self.state)
        self.assertFalse(target.exists())

    def test_exact_specialist_skill_upgrade_preserves_project_controls_and_host_settings(self):
        from loop_engineering.contracts import ROOT
        install(self.root, self.state)
        target = self.root / ".agents/skills/loop-engineering/SKILL.md"
        registry = self.root / ".codex/config.toml"
        before = registry.read_bytes()
        control = self.root / ".loop/project.json"
        atomic_write(control, b'{"frozen-project-control": true}\n')
        old = (ROOT / "tests/fixtures/native-host-v4-skill.md").read_bytes()
        atomic_write(target, old)
        result = install(self.root, self.state)
        self.assertEqual(result["files_written"], 1)
        self.assertEqual(result["files_upgraded"], [str(target)])
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(control.read_bytes(), b'{"frozen-project-control": true}\n')
        self.assertIn(b"verification_preflight", target.read_bytes())
        self.assertIn(b"registered_review", target.read_bytes())
        atomic_write(target, old + b'\nActual local review preference\n')
        with self.assertRaisesRegex(ContractError, "customized host file"):
            install(self.root, self.state)
        self.assertEqual(target.read_bytes(), old + b'\nActual local review preference\n')
        self.assertEqual(registry.read_bytes(), before)

    def test_invalid_json_toml_or_registry_is_refused_without_partial_skill_write(self):
        for host, name, raw in (("codex", ".codex/config.toml", "broken !"),
                               ("claude-code", ".mcp.json", "{} trailing"),
                               ("claude-code", ".mcp.json", '{"mcpServers": []}')):
            target = self.root / name; atomic_write(target, raw.encode())
            with self.subTest(host=host, raw=raw), self.assertRaises(ContractError):
                install(self.root, self.state, host)
            self.assertEqual(target.read_text(), raw)
            self.assertFalse((self.root / ".agents").exists())
            self.assertFalse((self.root / ".claude").exists())

    def test_symlink_registry_cannot_redirect_installation(self):
        outside = self.base / "outside.toml"; outside.write_text("# protected\n")
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").symlink_to(outside)
        with self.assertRaisesRegex(ContractError, "Symlink"):
            install(self.root, self.state)
        self.assertEqual(outside.read_text(), "# protected\n")
        self.assertFalse((self.root / ".agents").exists())

    def test_failed_install_rolls_back_its_writes_and_retry_is_safe(self):
        calls = 0
        def fail_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Offline interrupted installation")
            atomic_write(path, content)
        with patch("loop_engineering.host_install.atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                install(self.root, self.state)
        self.assertFalse((self.root / ".agents/skills/loop-engineering/SKILL.md").exists())
        self.assertEqual(install(self.root, self.state)["files_written"], 3)

    def test_existing_matching_server_preferences_are_preserved(self):
        config = configuration(self.root, self.state, "claude-code")
        entry = {**config["server_entry"], "env": {"CUSTOM_PREF": "preserve"}}
        original = json.dumps({"mcpServers": {"loop-native": entry}}, indent=1)
        (self.root / ".mcp.json").write_text(original)
        install(self.root, self.state, "claude-code")
        self.assertEqual((self.root / ".mcp.json").read_text(), original)

    def test_registry_change_during_merge_never_overwrites_new_user_preferences(self):
        from loop_engineering.host_install import _merge_config
        target = self.root / ".mcp.json"
        target.write_text('{"mcpServers": {}, "preference": "before"}')
        updated = '{"mcpServers": {}, "preference": "actual user change"}'
        def concurrent_change(original, config):
            result = _merge_config(original, config)
            target.write_text(updated)
            return result
        with patch("loop_engineering.host_install._merge_config", side_effect=concurrent_change):
            with self.assertRaisesRegex(ContractError, "registry changed"):
                install(self.root, self.state, "claude-code")
        self.assertEqual(target.read_text(), updated)
        self.assertFalse((self.root / ".claude").exists())

    def test_cli_exposes_one_time_host_install_and_read_only_preview(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["host-config", str(self.root), "--state-dir", str(self.state)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertFalse(json.loads(stdout.getvalue())["automatic_dispatch"])
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["host-install", str(self.root), "--state-dir", str(self.state)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("loop-native", stdout.getvalue())

    def test_missing_project_or_private_state_inside_workspace_is_refused(self):
        for project, state in ((self.base / "missing", self.state), (self.root, self.root / "state")):
            with self.subTest(project=project, state=state), self.assertRaises(ContractError):
                install(project, state)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
