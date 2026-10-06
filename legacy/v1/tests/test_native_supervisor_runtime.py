"""Offline approval-route admission, original ledger preservation and denials."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from loop_engineering.cli import parser
from loop_engineering.contracts import ContractError
from loop_engineering.native_supervisor import NativeSupervisor
from loop_engineering.native_supervisor_runtime import inspect_cli
from loop_engineering.processes import ProcessResult
from tests import test_native_supervisor as fixture


class SupervisorRuntimeTests(unittest.TestCase):
    setUp = fixture.NativeSupervisorTests.setUp
    start = fixture.NativeSupervisorTests.start
    controller = fixture.NativeSupervisorTests.controller
    advance = fixture.NativeSupervisorTests.advance
    create = fixture.NativeSupervisorTests.create
    executor = fixture.NativeSupervisorTests.executor

    def plan(self, supervisor, data, **overrides):
        team = self.service._data(self.team_id)
        candidate = self.service.team.snapshots.capture(self.root, self.service.team.profile(team))["digest"]
        return dict(update_id="actual-operator-runtime-route", expected_revision=data["revision"],
                    expected_candidate=candidate, approval_mode="auto-review",
                    authorization="Actual fixture operator authorizes official reviewed original verification only",
                    host_idle_confirmed=True) | overrides

    def test_signed_update_retains_session_limits_usage_team_and_failed_evidence(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        data = supervisor.journal.update(data["id"], "fixture.collected", lambda row: row.update(
            status="BLOCKED", reason="Original cleanup denied", session_id=fixture.SESSION,
            turns=1, dispatch_ms=100, known_tokens=15,
            last_report={"status": "blocked", "summary": "Original failed cleanup retained", "blocker": "permission", "questions": []}))
        team_before = deepcopy(self.service._data(self.team_id))
        args = self.plan(supervisor, data)
        result = supervisor.runtime_update(data["id"], **args)
        for key in ["id", "session_id", "deadline_epoch", "turns", "dispatch_ms", "known_tokens", "last_report", "reason", "status"]:
            self.assertEqual(result[key], data[key])
        self.assertEqual({k:v for k,v in result["policy"].items() if k != "approval_mode"},
                         {k:v for k,v in data["policy"].items() if k != "approval_mode"})
        self.assertFalse(result["runtime_capability"]["execution_capability_verified"])
        self.assertEqual(self.service._data(self.team_id), team_before)
        self.assertEqual(supervisor.journal.audit(data["id"]), result)
        replay = supervisor.runtime_update(data["id"], **args)
        self.assertTrue(replay.pop("runtime_update_replayed"))
        self.assertEqual(replay, result)
        with self.assertRaises(ContractError):
            supervisor.runtime_update(data["id"], **(args | {"authorization":"Different request"}))

    def test_guarded_global_flag_applies_to_resume_without_approval_bypass(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        result = supervisor.runtime_update(data["id"], **self.plan(supervisor, data))
        result["session_id"] = fixture.SESSION
        folder = self.store.run_dir(data["id"]) / "argv"; folder.mkdir()
        argv = self.driver.command(self.service, result, folder, self.service.progress(self.team_id))
        self.assertLess(argv.index("--approve-for-me"), argv.index("exec"))
        self.assertIn(fixture.SESSION, argv)
        self.assertNotIn("never", argv)
        # Official --approve-for-me selects workspace-write and rejects an
        # additional explicit sandbox flag, including on exec resume.
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--ask-for-approval", argv)
        for flag in ["--dangerously-bypass-approvals-and-sandbox", "danger-full-access", "--ignore-rules", "--ignore-user-config"]:
            self.assertNotIn(flag, argv)
        self.assertFalse(any("review_policy" in item for item in argv))

    def test_mutually_exclusive_cli_flags_fail_before_fixture_session_or_effect(self):
        from loop_engineering.contracts import ROOT
        result=subprocess.run([sys.executable,str(ROOT/"tests/fixtures/supervisor_codex.py"),
                               "--sandbox","workspace-write","--approve-for-me"],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,2)
        self.assertEqual(result.stdout,"")
        self.assertIn("cannot be used",result.stderr)

    def test_pending_process_pause_expiry_and_stale_revision_refuse(self):
        for change in [{"pending":{"pid":None}}, {"process":{"pid":123}},
                       {"signal":"PAUSED"}, {"deadline_epoch":0}]:
            with self.subTest(change=change):
                self.setUp(); self.start()
                supervisor = self.controller(); data = self.create(supervisor)
                changed = supervisor.journal.update(data["id"], "fixture.boundary", lambda row: row.update(change))
                with self.assertRaises(ContractError):
                    supervisor.runtime_update(data["id"], **self.plan(supervisor, changed))
                self.assertEqual(supervisor.journal.get(data["id"]), changed)
        self.setUp(); self.start()
        supervisor = self.controller(); data = self.create(supervisor)
        with self.assertRaises(ContractError):
            supervisor.runtime_update(data["id"], **self.plan(supervisor, data, expected_revision=data["revision"]+1))

    def test_wrong_candidate_missing_authorization_and_unstopped_writer_refuse(self):
        self.start()
        supervisor = self.controller(); data = self.create(supervisor)
        for overrides in [{"expected_candidate":"sha256:" + "0"*64}, {"authorization":""},
                          {"host_idle_confirmed":False}, {"approval_mode":"danger-full-access"}]:
            with self.subTest(overrides=overrides), self.assertRaises(ContractError):
                supervisor.runtime_update(data["id"], **self.plan(supervisor, data, **overrides))
        self.assertEqual(supervisor.journal.get(data["id"]), data)
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.service.worker_assign(self.team_id, "implement", packet["workbench"]["id"],
            "active-fixture", "backend", "Actual fixture owner", "Original fixture implementation", ["slug.py"], [])
        with self.assertRaisesRegex(ContractError, "specialist"):
            supervisor.runtime_update(data["id"], **self.plan(supervisor, data))

    def test_help_only_capability_inspection_has_no_execution_attestation(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)/"stdout"; err=Path(folder)/"stderr"; err.write_text("")
            out.write_text("--no-daemon --sandbox workspace-write --ask-for-approval --approve-for-me")
            fake = ProcessResult(0,"completed",10,out,err)
            driver=fixture.FixtureDriver()
            with patch("loop_engineering.native_supervisor_runtime.execute",return_value=fake) as call:
                result=inspect_cli(driver,"auto-review")
            self.assertTrue(result["route_available"])
            self.assertFalse(result["execution_capability_verified"])
            self.assertEqual(call.call_args.args[0],[driver.executable,"--help"])
            self.assertEqual(call.call_args.kwargs["timeout"],10)
            out.write_text("--no-daemon --sandbox workspace-write --ask-for-approval")
            with patch("loop_engineering.native_supervisor_runtime.execute",return_value=fake), self.assertRaises(ContractError):
                inspect_cli(driver,"auto-review")
            with patch("loop_engineering.native_supervisor_runtime.execute",return_value=ProcessResult(1,"completed",10,out,err)), self.assertRaises(ContractError):
                inspect_cli(driver,"never")

    def test_unsupported_route_refuses_before_create_or_another_model_turn(self):
        self.start()
        supervisor=self.controller(executor=lambda *a,**k:self.fail("No model dispatch"))
        with patch.object(self.driver,"preflight",side_effect=ContractError("Actual unsupported route")):
            with self.assertRaises(ContractError):
                self.create(supervisor,approval_mode="auto-review")
            self.assertEqual(supervisor.journal.rows(self.root),[])
            data=self.create(supervisor)
            result=supervisor.run(data["id"])
        self.assertEqual(result["status"],"BLOCKED")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["turns"],0)

    def test_candidate_change_during_help_refuses_without_runtime_journal_update(self):
        self.start(); supervisor=self.controller(); data=self.create(supervisor)
        arguments=self.plan(supervisor,data)
        def changed(mode):
            (self.root/"README.md").write_text("Actual fixture candidate changed during help\n")
            return {"route_available":True,"execution_capability_verified":False}
        with patch.object(self.driver,"preflight",side_effect=changed), self.assertRaisesRegex(ContractError,"candidate changed"):
            supervisor.runtime_update(data["id"],**arguments)
        self.assertEqual(supervisor.journal.get(data["id"]),data)

    def test_official_review_denial_remains_blocked_not_product_acceptance(self):
        self.start()
        report={"status":"blocked","summary":"Actual fixture reviewer denied required cleanup",
                "blocker":"permission","questions":["Actual environment required"]}
        def sleep(seconds):
            raise KeyboardInterrupt()
        supervisor=self.controller(executor=self.executor(report=report),sleeper=sleep)
        data=self.create(supervisor,approval_mode="auto-review")
        with self.assertRaises(KeyboardInterrupt): supervisor.run(data["id"])
        result=supervisor.journal.get(data["id"])
        self.assertEqual(result["last_report"],report)
        self.assertIsNone(result["pending"])
        self.assertEqual(result["turns"],1)
        self.assertNotEqual(self.service.progress(self.team_id)["status"],"COMPLETE")

    def test_cli_entry_updates_only_original_runtime_and_prompt_requires_actual_preflight(self):
        args=parser().parse_args(["host-supervisor-runtime",str(self.root),"--state-dir",str(self.store.directory),
            "--supervisor-id","supervisor-original","--update-id","original-route","--expected-revision","5",
            "--expected-candidate","sha256:"+"0"*64,"--approval-mode","auto-review",
            "--authorization","Actual operator","--host-idle"])
        self.assertEqual(args.command,"host-supervisor-runtime")
        self.assertTrue(args.host_idle)
        self.start(); supervisor=self.controller(); data=self.create(supervisor)
        prompt=supervisor._prompt(data,self.service.progress(self.team_id))
        self.assertIn("actual verification environment",prompt)
        self.assertIn("not execution evidence",prompt)
        self.assertIn("never signal a historical group ID",prompt)
        self.assertIn("official approval review",prompt)

    def test_updated_original_supervisor_finishes_actual_fixture_checks_in_same_session(self):
        self.start(); supervisor=self.controller(); data=self.create(supervisor)
        updated=supervisor.runtime_update(data["id"],**self.plan(supervisor,data))
        result=supervisor.run(updated["id"])
        self.assertEqual(result["status"],"COMPLETE",result["reason"])
        self.assertEqual(result["id"],data["id"])
        self.assertEqual(result["deadline_epoch"],data["deadline_epoch"])
        self.assertEqual(result["session_id"],fixture.SESSION)
        self.assertTrue(self.service.progress(self.team_id)["final_candidate_current"])
