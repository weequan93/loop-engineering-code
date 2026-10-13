import json
import os
import subprocess
import sys
import threading
import time

from loop_engineering import engine
from loop_engineering.api import Service
from loop_engineering.runner import Runner
from loop_engineering.util import LoopError

from tests.helpers import ProjectCase


def approve(store):
    store.mutate("goal.approved", lambda g, s: engine.approve(g, s, "test"))


class RunnerTests(ProjectCase):
    def run_goal(self, store, **kwargs):
        return Runner(self.project, store.id, log=lambda m: None, **kwargs).run()

    def test_autonomous_develop_to_done(self):
        store = self.goal()
        approve(store)
        result = self.run_goal(store)
        state = store.state()
        self.assertEqual(state["status"], "done", state["status_reason"])
        self.assertEqual(self.calls(), ["plan", "work"])
        self.assertEqual(result["turns"], 2)
        self.assertEqual(state["usage"]["turns"], 2)
        self.assertTrue(all(t["progress"] for t in state["turns"]))
        self.assertTrue(any(n["kind"] == "handoff" for n in state["notes"]))
        self.assertEqual(store.verify(), [])
        self.assertIsNotNone(store.runner()["exited_at"])

    def test_independent_review_fail_then_repair_then_pass(self):
        os.environ["FAKE_REVIEW"] = "fail-once"
        store = self.goal(reviews=[{"id": "code", "instructions": "Review the change"}])
        approve(store)
        self.run_goal(store)
        state = store.state()
        self.assertEqual(state["status"], "done", state["status_reason"])
        self.assertEqual(self.calls(), ["plan", "work", "review", "repair", "review"])
        self.assertEqual(state["reviews"]["code"]["verdict"], "pass")
        self.assertEqual(state["reviews"]["code"]["by"], "reviewer")
        prompt = (store.runs / "turn-0003" / "prompt.md").read_text()
        self.assertIn("independent reviewer", prompt)
        mcp = json.loads((store.runs / "turn-0003" / "mcp.json").read_text())
        self.assertIn("--review-token", mcp["mcpServers"]["loop"]["args"])  # reviewer-only MCP session
        self.assertIsNone(state["review_turn"])  # the one-time token expired with the turn

    def test_stagnation_blocks_without_burning_budget(self):
        os.environ["FAKE_MODE"] = "idle"
        store = self.goal()
        approve(store)
        self.run_goal(store)
        state = store.state()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("no observable progress", state["blocker"]["reason"])
        self.assertEqual(state["iteration"], 2)  # stagnation_limit=2

    def test_turn_limit_and_resume_grants_new_budget(self):
        os.environ["FAKE_MODE"] = "idle"
        store = self.goal(policy={"max_iterations": 1, "stagnation_limit": 5})
        approve(store)
        self.run_goal(store)
        self.assertEqual(store.state()["status"], "limit")
        Service(self.project, actor="human").control("resume")  # grants a fresh budget of 1 turn
        os.environ["FAKE_MODE"] = "fix"
        self.run_goal(store)
        state = store.state()
        self.assertEqual((state["status"], state["iteration"], len(state["tasks"])), ("limit", 2, 1))
        Service(self.project, actor="human").control("resume")
        self.run_goal(store)
        self.assertEqual(store.state()["status"], "done")

    def test_turn_timeout_is_recorded(self):
        os.environ["FAKE_MODE"] = "sleep"
        store = self.goal(policy={"turn_timeout_minutes": 1, "max_iterations": 1})
        approve(store)
        # Shrink the timeout through the policy multiplier for the test.
        import loop_engineering.runner as runner_mod
        original = runner_mod.procs.run

        def quick(*args, **kwargs):
            kwargs["timeout"] = 1
            return original(*args, **kwargs)
        runner_mod.procs.run = quick
        try:
            self.run_goal(store)
        finally:
            runner_mod.procs.run = original
        turn = store.state()["turns"][-1]
        self.assertEqual(turn["outcome"], "timeout")

    def test_stop_command_kills_running_turn(self):
        os.environ["FAKE_MODE"] = "sleep"
        store = self.goal()
        approve(store)
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(r=self.run_goal(store)))
        thread.start()
        deadline = time.time() + 20
        while time.time() < deadline and not (store.runner() or {}).get("turn", {}) or not (
                store.runner() or {}).get("turn", {}).get("pgid"):
            time.sleep(0.1)
        Service(self.project, actor="human").control("stop")
        thread.join(30)
        self.assertFalse(thread.is_alive())
        state = store.state()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["turns"][-1]["outcome"], "cancelled")

    def test_one_runner_per_goal(self):
        store = self.goal()
        approve(store)
        from loop_engineering.store import try_lock
        handle = try_lock(store.dir / "runner.lock")
        try:
            with self.assertRaises(LoopError):
                self.run_goal(store)
        finally:
            handle.close()

    def test_interrupted_turn_is_recorded_and_work_continues(self):
        store = self.goal()
        approve(store)
        store.write_runner({"pid": 999999, "identity": None, "turn": {"n": 1, "action": "plan",
                            "started_at": "2026-01-01T00:00:00+00:00", "pgid": None}, "exited_at": None})
        self.run_goal(store)
        state = store.state()
        self.assertEqual(state["turns"][0]["outcome"], "interrupted")
        self.assertEqual(state["status"], "done")

    def test_waiting_question_makes_no_model_calls(self):
        store = self.goal()
        approve(store)
        Service(self.project, actor="chat").ask("Need a decision", ["a", "b"])
        result = Runner(self.project, store.id, log=lambda m: None, max_turns=3).run()
        self.assertEqual(result["turns"], 0)
        self.assertEqual(self.calls(), [])

    def test_operate_goal_remediates_incident(self):
        store = self.goal(kind="operate", schedule={"interval_minutes": 60},
                          acceptance=[{"id": "health", "run": "test -f healthy"}])
        approve(store)
        runner = Runner(self.project, store.id, log=lambda m: None)
        runner.sleep = lambda s: setattr(runner, "stop_requested", True)  # stop at the first idle wait
        runner.run()
        state = store.state()
        self.assertEqual(self.calls(), ["remediate"])
        self.assertEqual(state["incidents"][-1]["status"], "resolved")
        self.assertEqual(state["status"], "idle")

    def test_background_start_and_cli(self):
        store = self.goal()
        approve(store)
        Service(self.project, actor="human").control("pause")  # start after a pause resumes it
        env = {**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1])}
        out = subprocess.run([sys.executable, "-m", "loop_engineering", "--project", str(self.root), "start"],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        deadline = time.time() + 60
        while time.time() < deadline and store.state()["status"] != "done":
            time.sleep(0.3)
        self.assertEqual(store.state()["status"], "done")
        line = subprocess.run([sys.executable, "-m", "loop_engineering", "--project", str(self.root), "statusline"],
                              capture_output=True, text=True, env=env, timeout=30).stdout
        self.assertIn("done", line)


class LongCheckTests(ProjectCase):
    def test_runner_waits_for_running_check_job_without_agent_turns(self):
        from loop_engineering import checks
        store = self.goal(acceptance=[{"id": "tests", "run": "python3 test_calc.py"}])
        approve(store)
        Service(self.project, actor="chat").plan([{"id": "slow", "title": "Slow check",
                                                    "checks": [{"id": "wait", "run": "sleep 2"}]}])
        job = checks.start_job(store, ["slow.wait"], "agent")
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["job"]), ("wait_job", job))
        runner = Runner(self.project, store.id, log=lambda m: None, max_turns=0)
        runner._wait_job(job)  # returns once the job finishes; no adapter involved
        self.assertEqual(store.state()["jobs"][job]["status"], "finished")
        self.assertEqual(self.calls(), [])
        self.assertNotEqual(engine.next_action(store.goal(), store.state())["kind"], "wait_job")


class QueueTests(ProjectCase):
    def make(self, goal_id, approve_it=True):
        store = self.project.create_goal({"id": goal_id, "kind": "develop", "title": goal_id.upper(),
                                          "objective": "add() must add",
                                          "acceptance": [{"id": "tests", "run": "python3 test_calc.py"}],
                                          "agent": {"adapter": "fake"}}, make_current=False)
        if approve_it:
            approve(store)
        return store

    def test_runner_continues_with_approved_goals_and_stops_before_unapproved(self):
        from loop_engineering import queue
        first = self.make("first")
        self.project.set_current("first")
        self.make("second")
        self.make("third", approve_it=False)
        queue.add(self.project, ["first", "second", "third"])
        result = Runner(self.project, "first", log=lambda m: None).run()
        self.assertEqual([g["status"] for g in result["goals"]], ["done", "done"])
        self.assertIn("third needs approval", result["queue"])
        self.assertEqual(self.project.current_id(), "second")
        self.assertEqual(self.project.goal("third").state()["status"], "draft")
        self.assertEqual(first.state()["status"], "done")
        status = (self.root / ".loop" / "STATUS.md").read_text()
        self.assertIn("Goal queue", status)
        self.assertIn("needs approval", status)

    def test_blocked_goal_does_not_advance_queue(self):
        from loop_engineering import queue
        os.environ["FAKE_MODE"] = "idle"
        self.make("first")
        self.project.set_current("first")
        self.make("second")
        queue.add(self.project, ["first", "second"])
        result = Runner(self.project, "first", log=lambda m: None).run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.project.goal("second").state()["status"], "ready")

    def test_enqueue_keeps_current_goal_and_project_lock(self):
        from loop_engineering import queue
        from loop_engineering.store import try_lock
        self.make("first")
        self.project.set_current("first")
        Service(self.project, actor="chat").goal_draft({"id": "later", "title": "Later", "objective": "o",
                                                        "acceptance": [{"id": "t", "run": "true"}]}, enqueue=True)
        self.assertEqual(self.project.current_id(), "first")
        self.assertEqual([r["id"] for r in queue.view(self.project)], ["later"])
        self.assertEqual(queue.next_after(self.project, "first"), "later")
        handle = try_lock(self.project.loop / "runner.lock")
        try:
            with self.assertRaises(LoopError):
                Runner(self.project, "first", log=lambda m: None).run()
        finally:
            handle.close()


class PlannerTests(ProjectCase):
    def test_runner_proposes_next_goals_once_when_queue_runs_dry(self):
        import json as _json
        from loop_engineering import queue, planner
        (self.root / ".loop" / "settings.json").write_text(_json.dumps({"propose_next": True, "propose_count": 2}))
        store = self.project.create_goal({"id": "first", "kind": "develop", "title": "First", "objective": "add",
                                          "acceptance": [{"id": "tests", "run": "python3 test_calc.py"}],
                                          "agent": {"adapter": "fake"}})
        approve(store)
        result = Runner(self.project, "first", log=lambda m: None).run()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["proposed"], ["next-1", "next-2"])  # limited to propose_count
        self.assertEqual([r["id"] for r in queue.view(self.project)], ["next-1", "next-2"])
        for goal_id in ("next-1", "next-2"):
            state = self.project.goal(goal_id).state()
            self.assertEqual(state["status"], "draft")  # never approved by the planner
            self.assertIsNone(state["approved_digest"])
        self.assertEqual(self.project.goal("first").goal()["title"], "First")  # cannot overwrite existing goals
        self.assertEqual(self.project.current_id(), "first")
        self.assertTrue(planner.already_proposed_after(self.project, "first"))
        prompt = next((self.root / ".loop" / "proposals").iterdir()) / "prompt.md"
        self.assertIn("planning agent", prompt.read_text())
        # A second run does not propose again for the same finished goal.
        again = Runner(self.project, "first", log=lambda m: None).run()
        self.assertNotIn("proposed", again)
        self.assertEqual(self.calls().count("propose"), 1)

    def test_planner_token_is_required(self):
        from loop_engineering.api import Service
        store = self.goal()
        with self.assertRaises(LoopError):
            Service(self.project, actor="planner", interactive=False, planner_token="forged").goal_draft(
                {"id": "x", "title": "x", "objective": "o", "acceptance": [{"id": "t", "run": "true"}]})
        self.assertEqual(self.project.goal_ids(), [store.id])


class PipelineTests(ProjectCase):
    def make(self, **extra):
        values = {"id": "full", "kind": "develop", "title": "Full", "objective": "add() must add",
                  "pipeline": {"skip": ["intake", "requirements_review", "solution", "test_design", "resources"]},
                  "acceptance": [{"id": "tests", "run": "python3 test_calc.py", "stage": "unit"},
                                 {"id": "lint", "run": "python3 -m py_compile calc.py", "stage": "integration"}],
                  "agent": {"adapter": "fake"}, "policy": {"max_iterations": 20, "stagnation_limit": 2}}
        values.update(extra)
        store = self.project.create_goal(values)
        approve(store)
        return store

    def test_full_pipeline_end_to_end_with_plan_review_rework(self):
        os.environ["FAKE_PLAN_REVIEW"] = "fail-once"
        store = self.make(pipeline="full", policy={"max_iterations": 40, "stagnation_limit": 2})
        Runner(self.project, store.id, log=lambda m: None).run()
        state = store.state()
        self.assertEqual(state["status"], "done", state["status_reason"])
        self.assertEqual(self.calls(), ["intake", "requirements", "review", "solution", "test_design", "plan",
                                        "resources", "review",            # plan review fails → back to plan
                                        "plan", "resources", "review",    # re-plan, re-check readiness, pass
                                        "work", "review", "review", "review", "review"])
        roles = [(t.get("stage"), t.get("role")) for t in state["turns"]]
        self.assertEqual(roles[:8], [("intake", "product"), ("requirements", "analyst"),
                                     ("requirements_review", "product"), ("solution", "architect"),
                                     ("test_design", "test-designer"), ("plan", "tech-lead"),
                                     ("resources", "resource-planner"), ("plan_review", "plan-reviewer")])
        self.assertEqual(roles[-5:], [("develop", "backend"), ("review", "reviewer"), ("review", "security"),
                                      ("final_acceptance", "acceptance-lead"), ("product_acceptance", "product")])
        self.assertEqual({k: v["verdict"] for k, v in state["reviews"].items()},
                         {"code-review": "pass", "security-review": "pass", "final-acceptance": "pass",
                          "product-acceptance": "pass"})
        self.assertTrue((self.root / "docs" / "loop" / store.id / "product-brief.md").is_file())
        plan_prompt = (store.runs / "turn-0009" / "prompt.md").read_text()
        self.assertIn("plan_review sent this stage back", plan_prompt)
        self.assertIn("R2 has no real test", plan_prompt)
        final_prompt = (store.runs / "turn-0015" / "prompt.md").read_text()
        self.assertIn("Traceability matrix", final_prompt)
        status = (self.root / ".loop" / "STATUS.md").read_text()
        self.assertIn("## Pipeline", status)
        from loop_engineering import status as status_mod
        view = status_mod.summary(store)["pipeline"]
        self.assertTrue(all(p["state"] == "done" for p in view))

    def test_pipeline_view_marks_completed_stages_while_waiting_for_a_human(self):
        from loop_engineering import status as status_mod
        store = self.make(id="waiting")
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R1", "text": "a", "source": "doc", "verify": "unit"}], None, True))
        Service(self.project, actor="chat").ask("Need a decision", ["a", "b"])
        view = {p["stage"]: p["state"] for p in status_mod.summary(store)["pipeline"]}
        self.assertEqual(view["requirements"], "done")
        self.assertEqual(view["plan"], "current")
        self.assertEqual(view["develop"], "pending")

    def test_pipeline_skip_runs_a_lighter_workflow(self):
        store = self.make(id="light", pipeline={"skip": ["intake", "requirements_review", "solution", "test_design",
                                                        "resources", "product-acceptance", "security-review"]})
        Runner(self.project, store.id, log=lambda m: None).run()
        self.assertEqual(store.state()["status"], "done", store.state()["status_reason"])
        self.assertEqual(self.calls(), ["requirements", "plan", "review", "work", "review", "review"])

    def test_plan_cannot_finalize_with_uncovered_requirements_or_unchecked_tasks(self):
        store = self.make()
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R1", "text": "a", "source": "doc", "verify": "unit"},
            {"id": "R2", "text": "b", "source": "doc", "verify": "unit"}], None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1"],
                                                              "checks": [{"id": "c", "run": "true"}]}], None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1", "R2"]}],
                                                       None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R9"]}], None))
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1", "R2"],
                                                          "checks": [{"id": "c", "run": "true"}]}], None, True))
        self.assertTrue(store.state()["plan_final"])
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["review"]), ("review", "plan_review"))
        # Changing requirements after the plan forces re-planning and a new plan review.
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R3", "text": "c", "source": "doc", "verify": "e2e"}], None, True))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "plan")

    def test_acceptance_runs_in_stage_order_and_profiles_pick_models(self):
        from loop_engineering import checks
        store = self.make(acceptance=[{"id": "e2e", "run": "true", "stage": "e2e"},
                                      {"id": "unit", "run": "true", "stage": "unit"},
                                      {"id": "perf", "run": "true", "stage": "performance"}],
                          agent={"adapter": "codex", "model": "cheap-coder",
                                 "stages": {"requirements": {"model": "thinker", "effort": "high"},
                                            "plan_review": {"adapter": "claude", "effort": "xhigh"}},
                                 "roles": {"architect": {"model": "thinker", "effort": "xhigh"}}})
        order = checks.select(store.goal(), store.state(), "acceptance", None)
        self.assertEqual(order, ["acceptance.unit", "acceptance.e2e", "acceptance.perf"])
        goal, state = store.goal(), store.state()
        req = engine.profile_for(goal, state, {"kind": "requirements"})
        self.assertEqual((req["adapter"], req["model"], req["effort"]), ("codex", "thinker", "high"))
        review = engine.profile_for(goal, state, {"kind": "review", "review": "plan_review"})
        self.assertEqual((review["adapter"], review["model"], review["effort"]), ("claude", "cheap-coder", "xhigh"))
        state["tasks"] = [{"id": "a", "role": "architect"}, {"id": "b", "role": "backend"}]
        arch = engine.profile_for(goal, state, {"kind": "work", "task": "a"})
        code = engine.profile_for(goal, state, {"kind": "work", "task": "b"})
        self.assertEqual((arch["model"], arch["effort"]), ("thinker", "xhigh"))
        self.assertEqual((code["model"], code["effort"]), ("cheap-coder", None))
        from loop_engineering import adapters
        codex = adapters.CodexAdapter({})
        codex._help = ""
        work = self.root / ".loop" / "w"
        work.mkdir()
        turn = codex.build("p", self.root, work, {**goal, "agent": {**goal["agent"], "model": "thinker",
                                                                    "effort": "max"}}, None)
        self.assertIn('model_reasoning_effort="max"', turn.argv)
        self.assertEqual(turn.argv[turn.argv.index("--model") + 1], "thinker")
        claude = adapters.ClaudeAdapter({}).build("p", self.root, work, {**goal, "agent": {
            **goal["agent"], "effort": "high"}}, None)
        self.assertEqual(claude.argv[claude.argv.index("--effort") + 1], "high")

    def test_new_drafts_default_to_full_pipeline_but_old_goals_keep_their_digest(self):
        from loop_engineering import model
        draft = Service(self.project, actor="chat").goal_draft({"title": "N", "objective": "o",
                                                                "acceptance": [{"id": "t", "run": "true"}]})
        self.assertEqual(draft["goal"]["pipeline"], "full")
        old = {"id": "old", "kind": "develop", "title": "O", "objective": "o",
               "acceptance": [{"id": "t", "run": "true"}]}
        self.assertIsNone(model.goal(old)["pipeline"])
        self.assertNotIn("pipeline", str(sorted(model.goal(old)["agent"])))


class RoleTests(ProjectCase):
    def test_role_guides_skills_and_profiles_reach_the_turn_prompt(self):
        from loop_engineering import prompts, roles
        (self.root / ".loop" / "roles").mkdir()
        (self.root / ".loop" / "roles" / "backend.md").write_text("# Role: backend (project)\nUse our ORM only.")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "ui-skill.md").write_text("Use the design tokens in tokens.css.")
        store = self.project.create_goal({
            "id": "roles", "title": "Roles", "objective": "o", "acceptance": [{"id": "t", "run": "true"}],
            "reviews": [{"id": "security-review", "instructions": "check auth"}],
            "agent": {"adapter": "fake", "roles": {
                "frontend": {"skill": "docs/ui-skill.md", "model": "ui-model"},
                "architect": {"skill": "system-design", "effort": "xhigh"}}}})
        approve(store)
        Service(self.project, actor="chat").plan([
            {"id": "api", "title": "API", "role": "backend"}, {"id": "ui", "title": "UI", "role": "frontend"},
            {"id": "design", "title": "Design", "role": "architect"}, {"id": "odd", "title": "Odd", "role": "ml"}])
        goal, state = store.goal(), store.state()

        def text(task):
            return prompts.turn_prompt(goal, state, {"kind": "work", "task": task, "summary": task}, 1, self.root)
        self.assertIn("Use our ORM only", text("api"))                  # project override wins
        self.assertIn("design tokens in tokens.css", text("ui"))         # repository skill file inlined
        self.assertIn("Role: frontend", text("ui"))                      # plus the built-in guide
        self.assertIn("`system-design` skill", text("design"))           # host skill by name
        self.assertIn("expert ml", text("odd"))                          # unknown role still gets guidance
        review = prompts.turn_prompt(goal, state, {"kind": "review", "review": "security-review", "summary": "r"},
                                     1, self.root)
        self.assertIn("Role: security", review)
        listing = roles.available(self.root)
        self.assertEqual(listing["backend"], ".loop/roles/backend.md")
        self.assertEqual(listing["tester"], "built-in")
        from loop_engineering import engine
        self.assertEqual(engine.profile_for(goal, state, {"kind": "work", "task": "ui"})["model"], "ui-model")


class WorkflowTeamTests(ProjectCase):
    def test_every_stage_has_a_role_and_projects_can_override(self):
        import json as _json
        from loop_engineering import engine, prompts
        store = self.project.create_goal({"id": "wf", "title": "WF", "objective": "o", "pipeline": "full",
                                          "acceptance": [{"id": "t", "run": "true"}], "agent": {"adapter": "fake"}})
        goal, state = store.goal(), store.state()
        roles = {kind: engine.profile_for(goal, state, act, self.root)["role"] for kind, act in {
            "requirements": {"kind": "requirements"}, "plan": {"kind": "plan"},
            "plan_review": {"kind": "review", "review": "plan_review"},
            "final": {"kind": "review", "review": "final-acceptance"}}.items()}
        self.assertEqual(roles, {"requirements": "analyst", "plan": "tech-lead", "plan_review": "plan-reviewer",
                                 "final": "acceptance-lead"})
        text = prompts.assignment(goal, state, {"kind": "requirements", "summary": "s"}, self.root)
        self.assertIn("## Workflow: Development", text)
        self.assertIn("**requirements**", text)
        self.assertIn("Role: requirements analyst", text)
        self.assertIn("**Produce:**", text)
        (self.root / ".loop" / "workflows").mkdir()
        (self.root / ".loop" / "workflows" / "develop.json").write_text(_json.dumps({"roles": {"plan": "architect"}}))
        self.assertEqual(engine.profile_for(goal, state, {"kind": "plan"}, self.root)["role"], "architect")
        operate = self.project.create_goal({"id": "ops", "kind": "operate", "title": "Ops", "objective": "o",
                                            "schedule": {"interval_minutes": 5},
                                            "acceptance": [{"id": "h", "run": "true"}], "agent": {"adapter": "fake"}})
        self.assertEqual(engine.profile_for(operate.goal(), operate.state(), {"kind": "remediate"}, self.root)["role"],
                         "sre")

    def test_task_team_staffing_and_coordinator_prompt(self):
        from loop_engineering import prompts
        store = self.project.create_goal({"id": "team", "title": "T", "objective": "o",
                                          "acceptance": [{"id": "t", "run": "true"}],
                                          "agent": {"adapter": "fake", "team_limit": 3,
                                                    "roles": {"backend": {"model": "coder-mini"}}}})
        approve(store)
        service = Service(self.project, actor="chat")
        with self.assertRaises(LoopError):  # 4 seats > team_limit 3
            service.plan([{"id": "big", "title": "Big", "team": [{"role": "backend", "count": 3},
                                                                 {"role": "tester", "count": 1}]}])
        service.plan([{"id": "api", "title": "API", "role": "backend",
                       "team": [{"role": "backend", "count": 2, "focus": "API + persistence"},
                                {"role": "tester", "count": 1}]}])
        goal, state = store.goal(), store.state()
        text = prompts.assignment(goal, state, {"kind": "work", "task": "api", "summary": "s"}, self.root)
        self.assertIn("You coordinate a specialist team (3 seats)", text)
        self.assertIn("2× **backend** — API + persistence", text)
        self.assertIn("model `coder-mini`", text)
        self.assertIn("spawn_agent", text)
        guide = store.runs / "roles" / "tester.md"
        self.assertTrue(guide.is_file())
        self.assertIn("Role: tester", guide.read_text())
        status = (self.root / ".loop" / "STATUS.md").read_text()
        self.assertIn("2×backend + 1×tester", status)


class GoalOrderTests(ProjectCase):
    def test_answering_a_queued_goal_runs_the_unfinished_current_goal_first(self):
        from loop_engineering import queue
        from loop_engineering.runner import goal_to_run
        current = self.project.create_goal({"id": "current", "title": "C", "objective": "o",
                                            "acceptance": [{"id": "t", "run": "true"}], "agent": {"adapter": "fake"}})
        self.project.create_goal({"id": "later", "title": "L", "objective": "o",
                                  "acceptance": [{"id": "t", "run": "true"}], "agent": {"adapter": "fake"}},
                                 make_current=False)
        queue.add(self.project, ["later"])
        self.assertEqual(goal_to_run(self.project, "later"), "current")
        current.mutate("done", lambda g, s: s.update(status="done"))
        self.assertEqual(goal_to_run(self.project, "later"), "later")


class SchedulingAndDriftTests(ProjectCase):
    def test_active_task_with_new_unfinished_dependency_does_not_starve_its_prerequisite(self):
        store = self.goal()
        approve(store)
        service = Service(self.project, actor="chat")
        service.plan([{"id": "main", "title": "Main"}])
        service.next()  # main becomes active
        service.plan([{"id": "prep", "title": "Prep"}, {"id": "main", "depends_on": ["prep"]}])
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["task"]), ("work", "prep"))
        with self.assertRaises(LoopError):
            Service(self.project, actor="agent", interactive=False).task("done", "main", "too early")

    def test_human_can_request_an_early_review(self):
        os.environ["FAKE_MODE"] = "fix"
        store = self.goal(reviews=[{"id": "code", "instructions": "Review"}])
        approve(store)
        Service(self.project, actor="chat").plan([{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
        Service(self.project, actor="human").request_review("code")
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["review"], act.get("early")), ("review", "code", True))
        Runner(self.project, store.id, log=lambda m: None, max_turns=1).run()
        state = store.state()
        self.assertEqual(state["reviews"]["code"]["verdict"], "pass")
        self.assertTrue(state["reviews"]["code"]["early"])
        self.assertEqual(state["review_requests"], [])
        self.assertEqual(engine.next_action(store.goal(), state)["kind"], "work")  # back to normal work

    def test_drift_detection_stops_busy_turns_without_results(self):
        os.environ["FAKE_MODE"] = "idle"
        (self.root / ".loop" / "settings.json").write_text(json.dumps({"propose_next": False, "drift_limit": 3}))
        store = self.goal(policy={"stagnation_limit": 50, "max_iterations": 20})
        approve(store)
        Service(self.project, actor="chat").plan([{"id": "t", "title": "T", "checks": [{"id": "c", "run": "true"}]}])
        # Idle turns that still touch files would fool the stagnation check; drift catches them.
        import loop_engineering.runner as runner_mod
        original = runner_mod.checks_mod.fingerprint
        counter = {"n": 0}

        def changing(root):
            counter["n"] += 1
            return f"fp-{counter['n']}"
        runner_mod.checks_mod.fingerprint = changing
        try:
            Runner(self.project, store.id, log=lambda m: None).run()
        finally:
            runner_mod.checks_mod.fingerprint = original
        state = store.state()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("no concrete result", state["blocker"]["reason"])
        self.assertEqual(state["iteration"], 3)
        Service(self.project, actor="human").unblock("re-planned")
        self.assertEqual(store.state()["turns_without_result"], 0)  # a human decision restarts the window

    def test_unblock_during_the_fingerprint_is_not_lost(self):
        store = self.goal()
        approve(store)
        store.mutate("test.blocked", lambda g, s: engine.block(g, s, "other", "drift", "x", "y", "runner"))
        import loop_engineering.runner as runner_mod
        original = runner_mod.checks_mod.fingerprint
        human = {"done": False}

        def slow(root):  # a human unblocks from Telegram while the runner fingerprints the tree
            if not human["done"]:
                human["done"] = True
                Service(self.project, actor="human").unblock("go on")
            return original(root)
        runner_mod.checks_mod.fingerprint = slow
        try:
            Runner(self.project, store.id, log=lambda m: None, max_turns=1).run()
        finally:
            runner_mod.checks_mod.fingerprint = original
        self.assertEqual(store.state()["iteration"], 1)  # it worked instead of exiting "Needs a human"

    def test_results_recorded_between_turns_count_as_progress(self):
        os.environ["FAKE_MODE"] = "idle"
        (self.root / ".loop" / "settings.json").write_text(json.dumps({"propose_next": False, "drift_limit": 3}))
        store = self.goal(policy={"stagnation_limit": 50, "max_iterations": 20})
        approve(store)
        Service(self.project, actor="chat").plan([{"id": "t", "title": "T", "checks": [{"id": "c", "run": "true"}]}])
        # A mark from before a background job finished a task: the first turn must see that result.
        store.mutate("test.mark", lambda g, s: s.update(progress_mark="before-the-job"))
        import loop_engineering.runner as runner_mod
        original, counter = runner_mod.checks_mod.fingerprint, {"n": 0}

        def changing(root):
            counter["n"] += 1
            return f"fp-{counter['n']}"
        runner_mod.checks_mod.fingerprint = changing
        try:
            Runner(self.project, store.id, log=lambda m: None).run()
        finally:
            runner_mod.checks_mod.fingerprint = original
        self.assertEqual(store.state()["iteration"], 4)  # turn 1 counted the result; three idle turns after it


class ReloadTests(ProjectCase):
    def test_runner_stops_between_turns_to_reload_new_code(self):
        from unittest import mock
        import loop_engineering.runner as runner_mod
        store = self.goal()
        approve(store)
        runner = Runner(self.project, store.id, log=lambda m: None, reload=True)
        with mock.patch.object(runner_mod, "code_stamp", return_value=runner.code + 1), \
                mock.patch.object(runner_mod.os, "execv") as execv:
            runner.run()
        self.assertEqual(store.state()["iteration"], 0)  # no turn started with stale code
        argv = execv.call_args[0][1]
        self.assertEqual(argv[2:5], ["loop_engineering", "run", "--project"])
        self.assertEqual(argv[-2:], ["--goal", store.id])
