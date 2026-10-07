import json

from loop_engineering import checks, engine, model
from loop_engineering.api import Service
from loop_engineering.util import LoopError

from tests.helpers import ProjectCase


class GoalLifecycleTests(ProjectCase):
    def test_draft_requires_acceptance_and_explicit_approval(self):
        service = Service(self.project, actor="chat")
        draft = service.goal_draft({"kind": "develop", "title": "x", "objective": "y"})
        self.assertTrue(draft["problems"])
        with self.assertRaises(LoopError):
            service.goal_approve(True)
        service.goal_draft({"acceptance": [{"id": "t", "run": "true"}]}, draft["goal"]["id"])
        with self.assertRaises(LoopError):
            service.goal_approve(False)
        status = service.goal_approve(True)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["next"]["kind"], "intake")  # new drafts use the full pipeline (product brief first)

    def test_contract_edit_requires_reapproval(self):
        store = self.goal()
        store.mutate("goal.approved", lambda g, s: engine.approve(g, s, "me"))
        raw = json.loads(store.goal_path.read_text())
        raw["acceptance"][0]["run"] = "true"
        store.goal_path.write_text(json.dumps(raw))
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual(act["kind"], "approve_goal")

    def test_autonomous_agents_cannot_approve_answer_or_unblock(self):
        self.goal()
        agent = Service(self.project, actor="agent", interactive=False)
        for call in (lambda: agent.goal_approve(True), lambda: agent.answer("q-x", "yes"),
                     lambda: agent.unblock("ok")):
            with self.assertRaises(LoopError):
                call()

    def test_full_develop_flow_with_task_checks_and_acceptance(self):
        store = self.goal()
        service = Service(self.project, actor="chat")
        service.goal_approve(True)
        service.plan([{"id": "fix", "title": "Fix", "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        self.assertEqual(service.next()["action"]["kind"], "work")
        self.assertEqual(engine.task_by_id(store.state(), "fix")["status"], "active")
        failed = service.task("done", "fix", "attempt", wait_seconds=30)
        self.assertEqual(failed["task"]["status"], "active")
        self.assertEqual(failed["checks"]["fix.unit"]["status"], "fail")
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        passed = service.task("done", "fix", "fixed", wait_seconds=30)
        self.assertEqual(passed["task"]["status"], "done")
        self.assertEqual(service.next()["action"]["kind"], "verify")
        result = service.finish("Fixed add", wait_seconds=30)
        self.assertEqual(result["goal_status"], "done")
        self.assertEqual(store.verify(), [])

    def test_task_attempt_limit_blocks_goal(self):
        store = self.goal(policy={"max_task_attempts": 2})
        service = Service(self.project, actor="chat")
        service.goal_approve(True)
        service.plan([{"id": "fix", "title": "Fix", "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        service.task("done", "fix", "a", wait_seconds=30)
        service.task("done", "fix", "b", wait_seconds=30)
        state = store.state()
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(engine.next_action(store.goal(), state)["kind"], "unblock")
        service.unblock("Investigated; retry with the real fix")
        self.assertEqual(engine.task_by_id(store.state(), "fix")["status"], "active")

    def test_acceptance_failure_repairs_then_blocks(self):
        store = self.goal(policy={"max_repair_rounds": 1})
        store.mutate("a", lambda g, s: engine.approve(g, s, "me"))
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T"}], None))
        store.mutate("d", lambda g, s: engine.claim_done(g, s, "t", "done"))
        engine.verify_now(store)
        stamp = checks.fingerprint(self.root)
        self.assertEqual(engine.next_action(store.goal(), store.state(), stamp)["kind"], "repair")
        (self.root / "other.txt").write_text("change")
        self.assertEqual(engine.next_action(store.goal(), store.state(), checks.fingerprint(self.root))["kind"],
                         "verify")
        engine.verify_now(store)
        self.assertEqual(store.state()["status"], "blocked")

    def test_permission_blocker_needs_reproduction_and_attempts(self):
        store = self.goal()
        with self.assertRaises(LoopError):
            store.mutate("b", lambda g, s: engine.block(g, s, "permission", "EPERM", "", ""))
        store.mutate("b", lambda g, s: engine.block(g, s, "permission", "EPERM", "killpg -> EPERM", "libproc census"))
        self.assertEqual(store.state()["status"], "blocked")

    def test_questions_wait_and_resume(self):
        store = self.goal()
        service = Service(self.project, actor="chat")
        service.goal_approve(True)
        q = service.ask("Which database?", ["postgres", "sqlite"])["question"]
        self.assertEqual(store.state()["status"], "waiting")
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "answer")
        service.answer(q["id"], "sqlite")
        self.assertEqual(store.state()["status"], "ready")

    def test_plan_rejects_cycles_and_unknown_deps(self):
        store = self.goal()
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "a", "title": "A", "depends_on": ["b"]},
                                                              {"id": "b", "title": "B", "depends_on": ["a"]}], None))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "a", "title": "A", "depends_on": ["zz"]}], None))
        self.assertEqual(store.state()["tasks"], [])

    def test_agent_and_human_reviews_gate_completion(self):
        store = self.goal(reviews=[{"id": "code", "by": "agent", "instructions": "Review the diff"},
                                   {"id": "ux", "by": "human", "instructions": "Click through the app"}])
        service = Service(self.project, actor="chat")
        service.goal_approve(True)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        service.plan([{"id": "t", "title": "T"}])
        service.task("done", "t", "done")
        engine.verify_now(store)
        stamp = checks.fingerprint(self.root)
        act = engine.next_action(store.goal(), store.state(), stamp)
        self.assertEqual((act["kind"], act["review"]), ("review", "code"))
        self.assertIn("independent", service.next()["assignment"])
        with self.assertRaises(LoopError):  # a chat agent cannot self-review
            Service(self.project, actor="agent", interactive=False).review("code", "pass")
        store.mutate("r", lambda g, s: engine.record_review(g, s, "code", "pass", "", stamp, "reviewer"))
        state = store.state()
        human = [q for q in state["questions"] if q.get("review") == "ux"]
        self.assertEqual(len(human), 1)
        self.assertEqual(state["status"], "waiting")
        service.answer(human[0]["id"], "approve")
        self.assertEqual(store.state()["status"], "done")

    def test_failed_review_becomes_repair(self):
        store = self.goal(reviews=[{"id": "code", "instructions": "Review"}])
        store.mutate("a", lambda g, s: engine.approve(g, s, "me"))
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T"}], None))
        store.mutate("d", lambda g, s: engine.claim_done(g, s, "t", "x"))
        engine.verify_now(store)
        stamp = checks.fingerprint(self.root)
        store.mutate("r", lambda g, s: engine.record_review(g, s, "code", "fail", "calc.py:1 bad", stamp, "rv"))
        act = engine.next_action(store.goal(), store.state(), stamp)
        self.assertEqual((act["kind"], act.get("review")), ("repair", "code"))


class OperateAndInvestigateTests(ProjectCase):
    def test_operate_cycles_incidents_and_recovery(self):
        store = self.goal(kind="operate", schedule={"interval_minutes": 5},
                          acceptance=[{"id": "health", "run": "test -f healthy"}])
        store.mutate("a", lambda g, s: engine.approve(g, s, "me"))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "health_check")
        engine.verify_now(store)
        state = store.state()
        self.assertEqual(len(state["incidents"]), 1)
        act = engine.next_action(store.goal(), state)
        self.assertEqual(act["kind"], "remediate")
        (self.root / "healthy").write_text("ok")
        store.mutate("m", lambda g, s: s["incidents"][-1].update(needs_recheck=True))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "health_check")
        engine.verify_now(store)
        state = store.state()
        self.assertEqual(state["incidents"][-1]["status"], "resolved")
        self.assertEqual(state["status"], "idle")
        self.assertEqual(engine.next_action(store.goal(), state)["kind"], "idle")
        self.assertTrue(store.goal()["policy"]["pause_on_interrupt"])

    def test_review_timeout_is_validated(self):
        self.assertEqual(model.review({"id": "r", "instructions": "x", "timeout_minutes": 10}, "r")["timeout_minutes"], 10)
        with self.assertRaises(LoopError):
            model.review({"id": "r", "instructions": "x", "timeout_minutes": 0}, "r")

    def test_operate_requires_schedule(self):
        with self.assertRaises(LoopError):
            model.goal({"kind": "operate", "title": "t", "objective": "o"})

    def test_investigate_findings_and_report(self):
        store = self.goal(kind="investigate", acceptance=[])
        store.mutate("a", lambda g, s: engine.approve(g, s, "me"))
        service = Service(self.project, actor="agent", interactive=False)
        self.assertEqual(service.next()["action"]["kind"], "plan")
        service.plan([{"id": "repro", "title": "Reproduce"}])
        hyp = service.note("hypothesis", "subtraction instead of addition")
        service.note("evidence", "add(2,3) returns -1", finding_id=hyp["finding"], finding_status="confirmed")
        service.task("done", "repro", "reproduced")
        self.assertEqual(service.next()["action"]["kind"], "report")
        service.finish("Root cause: '-' in calc.add")
        state = store.state()
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["findings"][0]["status"], "confirmed")


class StoreIntegrityTests(ProjectCase):
    def test_hash_chain_and_external_edit_detection(self):
        store = self.goal()
        self.assertEqual(store.verify(), [])
        state = json.loads(store.state_path.read_text())
        state["status"] = "done"
        store.state_path.write_text(json.dumps(state))
        self.assertIn("state.json was edited outside the controller", store.verify())
        with self.assertRaises(LoopError):
            store.mutate("x", lambda g, s: None)
        self.assertEqual(store.state()["status"], "blocked")
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "repair_state")
        Service(self.project, actor="human").repair("inspected")
        self.assertEqual(store.state()["integrity"], "ok")

    def test_status_files_are_rendered(self):
        store = self.goal()
        text = (self.root / ".loop" / "STATUS.md").read_text()
        self.assertIn("Fix add", text)
        self.assertIn("approve_goal", text)
        self.assertTrue(store.status_path.exists())

    def test_fingerprint_ignores_framework_files(self):
        before = checks.fingerprint(self.root)
        (self.root / ".loop" / "anything").write_text("x")
        (self.root / ".agents" / "skills" / "loop").mkdir(parents=True)
        (self.root / ".agents" / "skills" / "loop" / "SKILL.md").write_text("v2")
        self.assertEqual(before, checks.fingerprint(self.root))
        (self.root / "calc.py").write_text("changed")
        self.assertNotEqual(before, checks.fingerprint(self.root))


class NotifyTests(ProjectCase):
    def test_new_questions_and_blockers_notify_once(self):
        import os, sys
        out = self.fake_state / "notified.jsonl"
        (self.root / ".loop" / "notify.json").write_text(json.dumps({"desktop": False, "command": [
            sys.executable, "-c", f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')"]}))
        os.environ.pop("LOOP_NOTIFY")
        from loop_engineering.notify import config
        self.assertNotIn("telegram", config(self.root))  # isolated from the real user config
        store = self.goal()
        service = Service(self.project, actor="chat")
        service.goal_approve(True)
        q = service.ask("Which database?", ["postgres", "sqlite"])["question"]
        service.note("progress", "still waiting")  # another change must not re-notify
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], q["id"])
        self.assertIn("loop answer", rows[0]["command"])
        service.answer(q["id"], "sqlite")
        store.mutate("b", lambda g, s: engine.block(g, s, "decision", "Pick a vendor", "", ""))
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["title"].startswith("Blocked"))

    def test_progress_messages_once_per_turn(self):
        import os, sys
        out = self.fake_state / "progress.jsonl"
        (self.root / ".loop" / "notify.json").write_text(json.dumps({"desktop": False, "progress": True, "command": [
            sys.executable, "-c", f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')"]}))
        os.environ.pop("LOOP_NOTIFY")
        store = self.goal()
        for n in (1, 2):
            store.mutate("turn.finished", lambda g, s, n=n: s["turns"].append({
                "n": n, "action": "work", "outcome": "ok", "seconds": 60, "progress": True, "summary": f"did {n}"}))
            store.mutate("note", lambda g, s: engine.note(g, s, "progress", "x"))
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([r["title"].split(" ")[1] for r in rows], ["1", "2"])
        self.assertIn("did 2", rows[1]["message"])


class TelegramTests(ProjectCase):
    def test_telegram_channel_and_setup(self):
        import os
        from unittest import mock
        from loop_engineering import notify
        calls = []

        def fake(token, method, params, timeout=10.0):
            calls.append((token, method, dict(params)))
            if method == "getMe":
                return {"ok": True, "result": {"username": "loopbot"}}
            if method == "getUpdates":
                return {"ok": True, "result": [{"message": {"chat": {"id": 777}, "text": "/start"}}]}
            return {"ok": True}
        os.environ["XDG_CONFIG_HOME"] = str(self.fake_state / "cfg")
        with mock.patch.object(notify, "telegram_api", fake):
            result = notify.telegram_setup("123:ABC", wait_seconds=5, out=lambda m: None)
            self.assertEqual((result["chat_id"], result["test_message_sent"]), ("777", True))
            path = self.fake_state / "cfg" / "loop" / "notify.json"
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            os.environ.pop("LOOP_NOTIFY")
            (self.root / ".loop" / "notify.json").write_text(json.dumps({"desktop": False}))
            self.goal()
            Service(self.project, actor="chat").goal_approve(True)
            Service(self.project, actor="chat").ask("Ship to staging?", ["yes", "no"], kind="approval")
        sent = [c for c in calls if c[1] == "sendMessage"]
        self.assertTrue(any("Ship to staging?" in c[2]["text"] and c[2]["chat_id"] == "777" for c in sent))
        self.assertIn("notify.json", (self.root / ".loop" / ".gitignore").read_text())
