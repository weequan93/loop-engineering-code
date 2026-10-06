"""Real guarded binary/deletion effects, compatibility and interrupted recovery."""

import base64
from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch

from test_engine import LocalCase, FIXED
from loop_engineering.contracts import ContractError, load, validate_step
from loop_engineering.file_changes import change_bytes, change_from_bytes, MAX_BINARY_BYTES
from loop_engineering.workspace import byte_digest
from test_native import NativeCase, FixtureTransport
from loop_engineering.adapters import CodexDriver


class FileOperationTests(LocalCase):
    def configure_v3(self):
        task = load(self.task_path)
        task.setdefault("extensions", {})["agent_step_version"] = "0.3"
        task["scope"]["write_allow"] += ["obsolete.txt", "asset.bin"]
        self.write_task(task)
        (self.root / "obsolete.txt").write_text("old bytes")
        self.start()

    def proposal(self):
        step = self.step()
        step.update(schema_version="0.3", changes=[
            change_from_bytes("slug.py", byte_digest((self.root / "slug.py").read_bytes()), FIXED.encode(), "0.3"),
            change_from_bytes("obsolete.txt", byte_digest(b"old bytes"), None, "0.3"),
            change_from_bytes("asset.bin", None, b"\x89PNG\xff\x00", "0.3")])
        return step

    def test_v3_applies_binary_and_delete_with_actual_checks(self):
        self.configure_v3()
        data = self.engine.submit(self.run_id, self.proposal())
        self.assertEqual(data["state"]["status"], "SUCCEEDED")
        self.assertFalse((self.root / "obsolete.txt").exists())
        self.assertEqual((self.root / "asset.bin").read_bytes(), b"\x89PNG\xff\x00")
        self.assertEqual(self.store.replay(self.run_id), self.store.get(self.run_id))

    def test_version_must_be_explicitly_negotiated(self):
        self.start()
        proposal = self.step(); proposal["schema_version"] = "0.3"
        proposal["changes"] = [change_from_bytes("slug.py", byte_digest((self.root / "slug.py").read_bytes()), FIXED.encode(), "0.3")]
        with self.assertRaises(ContractError):
            self.engine.submit(self.run_id, proposal)
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_v2_remains_usable(self):
        self.start()
        self.assertEqual(self.engine.submit(self.run_id, self.step())["state"]["status"], "SUCCEEDED")

    def test_deleted_file_stale_hash_rejects_all_changes(self):
        self.configure_v3(); proposal = self.proposal()
        (self.root / "obsolete.txt").write_text("user change")
        with self.assertRaises(ContractError):
            self.engine.submit(self.run_id, proposal)
        self.assertFalse((self.root / "asset.bin").exists())
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_delete_cannot_escape_scope_or_modify_protected_tests(self):
        self.configure_v3()
        for name in ("../outside", ".loop/task.json", "test_slug.py"):
            step = self.proposal()
            step["changes"] = [change_from_bytes(name, byte_digest(b"old bytes"), None, "0.3")]
            with self.subTest(name=name), self.assertRaises(ContractError):
                self.engine.submit(self.run_id, step)

    def test_crash_after_delete_recovers_exact_journal(self):
        self.configure_v3()
        original = self.engine.snapshots.apply_prepared
        def interrupted(root, task, prepared):
            original(root, task, prepared[:2])
            raise RuntimeError("Injected interruption after deletion")
        with patch.object(self.engine.snapshots, "apply_prepared", interrupted), self.assertRaises(RuntimeError):
            self.engine.submit(self.run_id, self.proposal())
        self.assertFalse((self.root / "obsolete.txt").exists())
        self.engine.resume(self.run_id)
        self.assertEqual((self.root / "asset.bin").read_bytes(), b"\x89PNG\xff\x00")
        self.assertIsNone(self.store.get(self.run_id)["pending_edit"])
        self.assertEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")

    def test_recovery_refuses_user_recreation_of_deleted_path(self):
        self.configure_v3()
        original = self.engine.snapshots.apply_prepared
        def interrupted(root, task, prepared):
            original(root, task, prepared)
            raise RuntimeError("Injected interruption")
        with patch.object(self.engine.snapshots, "apply_prepared", interrupted), self.assertRaises(RuntimeError):
            self.engine.submit(self.run_id, self.proposal())
        (self.root / "obsolete.txt").write_text("user recreation")
        with self.assertRaisesRegex(ContractError, "user change"):
            self.engine.resume(self.run_id)
        self.assertEqual((self.root / "obsolete.txt").read_text(), "user recreation")

    def test_binary_limit_and_noncanonical_payloads_fail_closed(self):
        change = change_from_bytes("asset.bin", None, b"\xff", "0.3")
        for payload in ("/w=", "/w==\n", "/x==", "***", base64.b64encode(b"x" * (MAX_BINARY_BYTES + 1)).decode()):
            with self.subTest(payload=payload[:12]), self.assertRaises(ContractError):
                change_bytes({**change, "content": payload})

    def test_delete_needs_existing_precondition_and_null_payload(self):
        value = change_from_bytes("obsolete.txt", byte_digest(b"old"), None, "0.3")
        for mutation in ({"expected_sha256": None}, {"content": ""}, {"encoding": "utf-8"}):
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                change_bytes({**value, **mutation})

    def test_v2_integration_cannot_silently_encode_binary_or_deletion(self):
        for raw in (None, b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                change_from_bytes("asset.bin", byte_digest(b"old"), raw, "0.2")

    def test_codex_generation_uses_negotiated_v3_without_live_cli_dispatch(self):
        self.configure_v3()
        driver = CodexDriver(executable=sys.executable)
        driver.prepare_context(self.engine.context(self.run_id))
        destination = self.base / "codex-wire"; destination.mkdir()
        command = driver.command(self.root, destination)
        schema = load(destination / "step.schema.json")
        self.assertEqual(schema["properties"]["schema_version"]["enum"], ["0.3"])
        self.assertIn("operation", schema["properties"]["changes"]["items"]["properties"])
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_guarded_service_refuses_unnegotiated_modern_changes(self):
        self.start()
        change = change_from_bytes("slug.py", byte_digest((self.root / "slug.py").read_bytes()), None, "0.3")
        with self.assertRaisesRegex(ContractError, "negotiated"):
            self.engine.snapshots.prepare_changes(self.root, load(self.task_path), [change])


class NativeFileOperationTests(NativeCase):
    def test_direct_provider_fixture_generates_and_verifies_v3_binary_and_deletion(self):
        task = load(self.task_path); task.setdefault("extensions", {})["agent_step_version"] = "0.3"
        task["scope"]["write_allow"] += ["obsolete.txt", "asset.bin"]
        self.write_task(task); (self.root / "obsolete.txt").write_text("old")
        self.start()
        class Transport(FixtureTransport):
            def post(inner, operation, payload, **kwargs):
                response = super(Transport, inner).post(operation, payload, **kwargs)
                if operation == "respond":
                    self.assertEqual(payload["text"]["format"]["schema"]["properties"]["schema_version"]["enum"], ["0.3"])
                    item = response["output"][0]["content"][0]
                    step = json.loads(item["text"]); step["schema_version"] = "0.3"
                    step["changes"] = [change_from_bytes(c["path"], c["expected_sha256"], c["new_content"].encode(), "0.3") for c in step["changes"]]
                    step["changes"] += [change_from_bytes("obsolete.txt", byte_digest(b"old"), None, "0.3"),
                                        change_from_bytes("asset.bin", None, b"\xff\x00", "0.3")]
                    item["text"] = json.dumps(step)
                return response
        result = self.engine.drive(self.run_id, transport=Transport(self.engine, self.run_id))
        self.assertEqual(result["state"]["status"], "SUCCEEDED", result["state"]["reason"])
        self.assertFalse((self.root / "obsolete.txt").exists())
        self.assertEqual((self.root / "asset.bin").read_bytes(), b"\xff\x00")
