import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

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

    def test_queue_starts_from_its_head_and_passes_over_blocked_goals(self):
        from loop_engineering import queue
        for goal_id in ("f05", "f06", "f07"):
            self.make(goal_id)
        queue.add(self.project, ["f05", "f06", "f07"])
        # F05 sits ahead of the goal that just finished: it is next, not skipped (the F05-behind-F06 trap).
        self.assertEqual(queue.next_after(self.project, "f06"), "f05")
        self.project.goal("f05").mutate("b", lambda g, s: engine.block(g, s, "other", "waits", "", "", "human"))
        self.assertEqual(queue.next_after(self.project, "f06"), "f07")   # a blocked goal waits for its person

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
                  "pipeline": "full",
                  "acceptance": [{"id": "tests", "run": "python3 test_calc.py", "stage": "unit"},
                                 {"id": "lint", "run": "python3 -m py_compile calc.py", "stage": "integration"}],
                  "agent": {"adapter": "fake"}, "policy": {"max_iterations": 20, "stagnation_limit": 2}}
        values.update(extra)
        store = self.project.create_goal(values)
        approve(store)
        return store

    def test_full_pipeline_end_to_end_with_design_review_rework(self):
        os.environ["FAKE_DESIGN_REVIEW"] = "fail-once"
        store = self.make(policy={"max_iterations": 40, "stagnation_limit": 2})
        Runner(self.project, store.id, log=lambda m: None).run()
        state = store.state()
        self.assertEqual(state["status"], "done", state["status_reason"])
        self.assertEqual(self.calls(), ["brief", "design", "review",   # design review fails → back to design
                                        "design", "review",              # revised design passes
                                        "work", "review"])               # one combined final review
        roles = [(t.get("stage"), t.get("role")) for t in state["turns"]]
        self.assertEqual(roles, [("brief", "analyst"), ("design", "tech-lead"), ("design_review", "plan-reviewer"),
                                 ("design", "tech-lead"), ("design_review", "plan-reviewer"),
                                 ("develop", "backend"), ("final_review", "acceptance-lead")])
        self.assertEqual({k: v["verdict"] for k, v in state["reviews"].items()}, {"final-review": "pass"})
        self.assertTrue((self.root / "docs" / "loop" / store.id / "brief.md").is_file())
        self.assertTrue((self.root / "docs" / "loop" / store.id / "design.md").is_file())
        design_prompt = (store.runs / "turn-0004" / "prompt.md").read_text()
        self.assertIn("design_review sent this stage back", design_prompt)
        self.assertIn("R2 has no real test", design_prompt)
        final_prompt = (store.runs / "turn-0007" / "prompt.md").read_text()
        self.assertIn("Traceability", final_prompt)
        for angle in ("**requirements**", "**code**", "**security**", "**feature**"):
            self.assertIn(angle, final_prompt)
        self.assertNotIn("**performance**", final_prompt)          # only when the goal is about performance
        status = (self.root / ".loop" / "STATUS.md").read_text()
        self.assertIn("## Pipeline", status)
        from loop_engineering import status as status_mod
        view = status_mod.summary(store)["pipeline"]
        self.assertEqual([p["stage"] for p in view], ["brief", "design", "design_review", "develop", "test",
                                                      "final-review", "done"])
        self.assertTrue(all(p["state"] == "done" for p in view))

    def test_design_review_goes_to_a_person_after_two_rounds(self):
        os.environ["FAKE_DESIGN_REVIEW"] = "fail-always"
        store = self.make(policy={"max_iterations": 40, "stagnation_limit": 3})
        Runner(self.project, store.id, log=lambda m: None).run()
        state = store.state()
        self.assertEqual(self.calls(), ["brief", "design", "review", "design", "review"])
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["blocker"]["category"], "decision")
        self.assertIn("loop review design_review pass", state["blocker"]["reason"])
        # The person accepts the design as it is: the blocker clears and development starts.
        Service(self.project, actor="human").review("design_review", "pass", "accepted by the owner")
        state = store.state()
        self.assertIsNone(state["blocker"])
        self.assertEqual(engine.next_action(store.goal(), state)["kind"], "design")  # the last fail reset the design
        Runner(self.project, store.id, log=lambda m: None).run()
        self.assertEqual(store.state()["status"], "done", store.state()["status_reason"])
        self.assertEqual(self.calls()[5:], ["design", "work", "review"])  # no third design review

    def test_pipeline_view_marks_completed_stages_while_waiting_for_a_human(self):
        from loop_engineering import status as status_mod
        store = self.make(id="waiting")
        store.mutate("b", lambda g, s: s.setdefault("stages", {}).update(
            brief={"done": True, "path": "docs/loop/waiting/brief.md", "digest": "x"}))
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R1", "text": "a", "source": "doc", "verify": "unit"}], None, True))
        Service(self.project, actor="chat").ask("Need a decision", ["a", "b"])
        view = {p["stage"]: p["state"] for p in status_mod.summary(store)["pipeline"]}
        self.assertEqual(view["brief"], "done")
        self.assertEqual(view["design"], "current")
        self.assertEqual(view["develop"], "pending")

    def test_pipeline_skip_runs_a_lighter_workflow(self):
        store = self.make(id="light", pipeline={"skip": ["design_review", "final-review"]})
        Runner(self.project, store.id, log=lambda m: None).run()
        self.assertEqual(store.state()["status"], "done", store.state()["status_reason"])
        self.assertEqual(self.calls(), ["brief", "design", "work"])

    def test_plan_cannot_finalize_with_uncovered_requirements_or_unchecked_tasks(self):
        store = self.make()
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R1", "text": "a", "source": "doc", "verify": "unit"},
            {"id": "R2", "text": "b", "source": "doc", "verify": "unit"}], None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1"],
                                                              "checks": [{"id": "c", "run": "test -d ."}]}], None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1", "R2"]}],
                                                       None, True))
        with self.assertRaises(LoopError):
            store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R9"]}], None))
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1", "R2"],
                                                          "checks": [{"id": "c", "run": "test -d ."}]}], None, True))
        self.assertTrue(store.state()["plan_final"])
        for sid in ("brief", "design"):
            store.mutate("d", lambda g, s, sid=sid: s.setdefault("stages", {}).update({sid: {"done": True}}))
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["review"]), ("review", "design_review"))
        # Before the design review, any change re-opens the plan for review.
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R3", "text": "c", "source": "doc", "verify": "e2e"}], None, True))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "design")

    def test_an_accepted_design_stays_accepted_unless_coverage_breaks(self):
        store = self.make(id="accepted")
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R1", "text": "a", "source": "doc", "verify": "unit"}], None, True))
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T", "covers": ["R1"],
                                                          "checks": [{"id": "c", "run": "test -d ."}]}], None, True))
        for sid in ("brief", "design"):
            store.mutate("d", lambda g, s, sid=sid: s.setdefault("stages", {}).update({sid: {"done": True}}))
        Service(self.project, actor="human").review("design_review", "pass", "")
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "work")
        # A task edit during development keeps the design accepted: no new plan stage, no new review.
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "t", "title": "T v2", "covers": ["R1"],
                                                          "checks": [{"id": "c", "run": "test -d ."}]}], None))
        self.assertTrue(store.state()["plan_final"])
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "work")
        # A new requirement nobody covers sends the design back, but the review is not repeated.
        store.mutate("r", lambda g, s: engine.set_requirements(g, s, [
            {"id": "R2", "text": "b", "source": "doc", "verify": "unit"}], None, True))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "design")
        store.mutate("p", lambda g, s: engine.plan(g, s, [{"id": "u", "title": "U", "covers": ["R2"],
                                                          "checks": [{"id": "c", "run": "test -d ."}]}], None, True))
        self.assertEqual(engine.next_action(store.goal(), store.state())["kind"], "work")

    def test_failed_reviews_and_resubmissions_are_not_progress(self):
        from loop_engineering.runner import progress_mark
        state = {"tasks": [], "checks": {}, "reviews": {}, "stages": {}, "incidents": []}
        before = progress_mark(state)
        state["stages"]["design_review"] = {"verdict": "fail", "rounds": 1, "at": "t1"}
        state["reviews"]["final-review"] = {"verdict": "fail", "at": "t2"}
        self.assertEqual(progress_mark(state), before)
        state["reviews"]["final-review"] = {"verdict": "pass", "at": "t3"}
        self.assertNotEqual(progress_mark(state), before)

    def test_acceptance_runs_in_stage_order_and_profiles_pick_models(self):
        from loop_engineering import checks
        store = self.make(acceptance=[{"id": "e2e", "run": "true", "stage": "e2e"},
                                      {"id": "unit", "run": "true", "stage": "unit"},
                                      {"id": "perf", "run": "true", "stage": "performance"}],
                          agent={"adapter": "codex", "model": "cheap-coder",
                                 "stages": {"brief": {"model": "thinker", "effort": "high"},
                                            "design_review": {"adapter": "claude", "effort": "xhigh"}},
                                 "roles": {"architect": {"model": "thinker", "effort": "xhigh"}}})
        order = checks.select(store.goal(), store.state(), "acceptance", None)
        self.assertEqual(order, ["acceptance.unit", "acceptance.e2e", "acceptance.perf"])
        goal, state = store.goal(), store.state()
        req = engine.profile_for(goal, state, {"kind": "brief"})
        self.assertEqual((req["adapter"], req["model"], req["effort"]), ("codex", "thinker", "high"))
        review = engine.profile_for(goal, state, {"kind": "review", "review": "design_review"})
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
            "brief": {"kind": "brief"}, "design": {"kind": "design"},
            "design_review": {"kind": "review", "review": "design_review"},
            "final": {"kind": "review", "review": "final-review"}}.items()}
        self.assertEqual(roles, {"brief": "analyst", "design": "tech-lead", "design_review": "plan-reviewer",
                                 "final": "acceptance-lead"})
        text = prompts.assignment(goal, state, {"kind": "brief", "summary": "s", "output": "docs/loop/wf/brief.md"},
                                  self.root)
        self.assertIn("## Workflow: Development", text)
        self.assertIn("**brief**", text)
        self.assertIn("Role: requirements analyst", text)
        self.assertIn("**Produce:**", text)
        (self.root / ".loop" / "workflows").mkdir()
        (self.root / ".loop" / "workflows" / "develop.json").write_text(_json.dumps({"roles": {"design": "architect"}}))
        self.assertEqual(engine.profile_for(goal, state, {"kind": "design"}, self.root)["role"], "architect")
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
        Service(self.project, actor="chat").plan([{"id": "t", "title": "T", "checks": [{"id": "c", "run": "test -d ."}]}])
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
        self.assertEqual(state["iteration"], 6)  # turn 3 drifted: automatic refocus; turn 6 drifted again: pause
        self.assertTrue(any(n["by"] == "controller" and "Controller refocus" in n["text"] for n in state["notes"]))
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
        Service(self.project, actor="chat").plan([{"id": "t", "title": "T", "checks": [{"id": "c", "run": "test -d ."}]}])
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
        self.assertEqual(store.state()["iteration"], 7)  # turn 1 counted; drift at 4 refocuses, at 7 pauses


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


class ServiceRefusalTests(ProjectCase):
    def test_out_of_credits_blocks_at_once_and_capacity_backs_off_without_stagnation(self):
        os.environ["FAKE_MODE"] = "credits"
        store = self.goal(policy={"stagnation_limit": 2, "max_iterations": 10})
        approve(store)
        Runner(self.project, store.id, log=lambda m: None).run()
        state = store.state()
        self.assertEqual((state["iteration"], state["blocker"]["category"]), (1, "environment"))
        self.assertIn("out of credits", state["blocker"]["reason"])
        self.assertEqual(state.get("stagnant_turns", 0), 0)
        os.environ["FAKE_MODE"] = "busy"
        Service(self.project, actor="human").unblock("credits added")
        sleeps = []
        Runner(self.project, store.id, log=lambda m: None, max_turns=2, sleep=sleeps.append).run()
        state = store.state()
        self.assertIsNone(state["blocker"])           # capacity is transient: never a stagnation block
        self.assertEqual(state.get("stagnant_turns", 0), 0)
        self.assertIn(300, sleeps)


class FirstPlanTests(ProjectCase):
    def test_planner_drafts_the_first_goals_of_a_new_project(self):
        from loop_engineering import planner, queue
        self.assertEqual(self.project.goal_ids(), [])
        entry = planner.propose(self.project, count=2, adapter="fake")
        self.assertEqual(entry["created"], ["next-1", "next-2"])
        self.assertEqual([r["id"] for r in queue.view(self.project)], ["next-1", "next-2"])
        self.assertIsNone(self.project.goal("next-1").state()["approved_digest"])  # still needs the human
        prompt = next((self.root / ".loop" / "proposals").iterdir()) / "prompt.md"
        self.assertIn("this is a new project", prompt.read_text())
        self.assertEqual(self.project.current_id(), "next-1")


class TaskSessionTests(ProjectCase):
    """fresh_session_each_turn="task": consecutive turns on the same task resume one session with a short prompt."""

    def test_policy_accepts_task_mode_and_existing_values(self):
        from loop_engineering import model
        self.assertEqual(model.policy({"fresh_session_each_turn": "task"})["fresh_session_each_turn"], "task")
        self.assertIs(model.policy({})["fresh_session_each_turn"], True)
        with self.assertRaises(LoopError):
            model.policy({"fresh_session_each_turn": "always"})

    def test_session_is_reused_only_for_the_same_task_contract_and_within_the_bound(self):
        from loop_engineering.runner import TASK_SESSION_TURNS, next_task_session, task_session
        state = {"approved_digest": "d1", "task_session": {"task": "a", "kind": "work", "session_id": "S",
                                                             "digest": "d1", "turns": 1}}
        work_a = {"kind": "work", "task": "a"}
        self.assertEqual(task_session(state, work_a), "S")
        self.assertIsNone(task_session(state, {"kind": "work", "task": "b"}))        # another task: fresh
        self.assertIsNone(task_session(state, {"kind": "review", "review": "code"}))  # reviews never resume
        self.assertIsNone(task_session({**state, "approved_digest": "d2"}, work_a))   # contract changed
        state["task_session"]["turns"] = TASK_SESSION_TURNS
        self.assertIsNone(task_session(state, work_a))                                 # bounded context
        record = next_task_session({"approved_digest": "d1"}, {"turns": 2}, work_a, "S2", resumed=True)
        self.assertEqual(record, {"task": "a", "kind": "work", "session_id": "S2", "digest": "d1", "turns": 3})
        self.assertEqual(next_task_session({"approved_digest": "d1"}, None, work_a, "S3", resumed=False)["turns"], 1)
        self.assertIsNone(next_task_session({}, None, {"kind": "plan"}, "S", resumed=False))

    def test_runner_resumes_the_task_session_with_a_continuation_prompt(self):
        from unittest import mock
        from loop_engineering import adapters as adapters_mod
        store = self.goal(policy={"max_iterations": 10, "stagnation_limit": 2, "fresh_session_each_turn": "task"})
        approve(store)
        Service(self.project, actor="agent", interactive=False).plan(
            [{"id": "fix", "title": "Fix", "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        digest = store.state()["approved_digest"]
        store.mutate("seed", lambda g, s: s.update(task_session={"task": "fix", "kind": "work", "session_id": "S1",
                                                                 "digest": digest, "turns": 1}))
        seen = []
        real_get = adapters_mod.get

        def stub_get(name, root):
            adapter = real_get(name, root)

            class Stub(type(adapter)):
                def build(self, prompt, project, workdir, goal, session, **kw):
                    seen.append((session, prompt))
                    return super().build(prompt, project, workdir, goal, session, **kw)

                def parse(self, stdout, stderr, workdir):
                    report = super().parse(stdout, stderr, workdir)
                    report.session_id = "S1"
                    return report
            return Stub(adapter.config)
        with mock.patch.object(adapters_mod, "get", stub_get):
            Runner(self.project, store.id, log=lambda m: None, max_turns=1).run()
        session, prompt = seen[0]
        self.assertEqual(session, "S1")
        self.assertIn("continuing in this session", prompt)
        self.assertIn("Task `fix`", prompt)
        self.assertNotIn("## Loop protocol", prompt)          # the session already has it
        self.assertEqual(store.state()["task_session"]["turns"], 2)


class ContextDietTests(ProjectCase):
    def test_long_goal_goes_by_reference_and_goal_md_holds_the_full_text(self):
        from loop_engineering import prompts, status
        long_rule = "Never touch production data. " + "Details about the staging copy and its refresh. " * 40
        store = self.goal(constraints=[long_rule, "Use Docker for integration tests. Limits are set."] * 3)
        approve(store)
        for i in range(5):
            store.mutate("q", lambda g, s, i=i: s["questions"].append(
                {"id": f"q{i}", "kind": "question", "text": f"question {i}", "answer": f"answer {i}",
                 "answered_at": "2026-01-01T00:00:00Z", "blocking": False}))
        goal, state = store.goal(), store.state()
        block = prompts.context_block(goal, state)
        self.assertIn(f".loop/goals/{goal['id']}/GOAL.md", block)
        self.assertIn("1. Never touch production data.", block)
        self.assertNotIn("Details about the staging copy", block)
        self.assertNotIn("answer 1", block)                       # only the latest three answers
        self.assertIn("answer 4", block)
        status.render_files(store)
        full = (store.dir / "GOAL.md").read_text()
        self.assertIn(long_rule.strip(), full)                    # nothing lost: the full text is one read away
        self.assertIn("answer 0", full)

    def test_task_working_set_reaches_the_assignment(self):
        from loop_engineering import prompts
        store = self.goal()
        approve(store)
        Service(self.project, actor="agent", interactive=False).plan(
            [{"id": "fix", "title": "Fix", "reads": ["calc.py", "docs/spec.md#add"],
              "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        with self.assertRaises(LoopError):
            Service(self.project, actor="agent", interactive=False).plan([{"id": "bad", "title": "B", "reads": "x"}])
        text = prompts.turn_prompt(store.goal(), store.state(), {"kind": "work", "task": "fix", "summary": "f"}, 1,
                                   self.root)
        self.assertIn("Read first (working set", text)
        self.assertIn("`docs/spec.md#add`", text)
        self.assertIn("rg -n", text)                              # locate-then-read rule
        self.assertIn("handoff note is the report", text)

    def test_each_turn_records_its_context_bill(self):
        store = self.goal()
        approve(store)
        Runner(self.project, store.id, log=lambda m: None).run()
        turn = store.state()["turns"][0]
        self.assertGreater(turn["context"]["prompt_chars"], 0)
        self.assertEqual(set(turn["context"]["sections"]), {"intro", "goal", "assignment", "playbook", "protocol"})
        bill = json.loads((store.runs / "turn-0001" / "context.json").read_text())
        self.assertEqual(bill["prompt_chars"], turn["context"]["prompt_chars"])
        from loop_engineering import dashboard
        self.assertIn("context", dashboard.turns(store)["turns"][-1])


class QuietWindowTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.store = self.goal()
        approve(self.store)
        Service(self.project, actor="agent", interactive=False).plan(
            [{"id": "perf", "title": "Measure", "needs": ["exclusive-machine"],
              "checks": [{"id": "c", "run": "test -d ."}]}])

    def at(self, hour):
        from datetime import datetime
        return datetime(2026, 10, 18, hour, 30).timestamp()

    def test_without_a_window_the_runner_stops_for_a_person(self):
        self.assertEqual(engine.next_action(self.store.goal(), self.store.state())["kind"], "wait_window")
        self.assertIsNone(self.store.state()["blocker"])        # waiting for a window is not a blocker
        result = Runner(self.project, self.store.id, log=lambda m: None).run()
        self.assertIn("quiet window", result["reason"])

    def test_the_window_runs_the_task_alone_and_holds_the_machine(self):
        settings = json.loads((self.root / ".loop" / "settings.json").read_text())
        settings["quiet_window"] = {"start": "01:00", "end": "06:00", "max_load": 999}
        (self.root / ".loop" / "settings.json").write_text(json.dumps(settings))
        clock = {"t": self.at(2)}
        runner = Runner(self.project, self.store.id, log=lambda m: None, clock=lambda: clock["t"],
                        sleep=lambda s: None)
        self.assertTrue(runner._machine_step(self.store.state()))
        task = engine.task_by_id(self.store.state(), "perf")
        self.assertEqual((task["status"], task["window_run"]), ("pending", True))
        self.assertIsNotNone(runner.machine)
        act = engine.next_action(self.store.goal(), self.store.state())
        self.assertEqual((act["task"], act["batch"]), ("perf", []))
        # Another project's runner sees the machine is taken and waits.
        other = Runner(self.project, self.store.id, log=lambda m: None, sleep=lambda s: None)
        self.assertEqual(other._machine_holder(), str(self.project.root))
        clock["t"] = self.at(7)                                  # the window ends before the task is done
        runner._machine_step(self.store.state())
        self.assertEqual(engine.task_by_id(self.store.state(), "perf")["status"], "deferred")
        self.assertIsNone(runner.machine)
        self.assertIsNone(other._machine_holder())


class FrozenCheckTests(ProjectCase):
    def test_task_checks_run_on_a_frozen_copy_while_other_work_goes_on(self):
        import sys as _sys
        import unittest
        if _sys.platform != "darwin":
            raise unittest.SkipTest("frozen copies use APFS clonefile")
        from loop_engineering import checks
        store = self.goal()
        approve(store)
        agent = Service(self.project, actor="agent", interactive=False)
        agent.plan([{"id": "a", "title": "A", "checks": [{"id": "c", "run": "sleep 2 && test -f a.txt"}]},
                    {"id": "b", "title": "B", "checks": [{"id": "c", "run": "test -d ."}]}])
        (self.root / "a.txt").write_text("done")
        claim = agent.task("done", "a", "made a.txt", wait_seconds=0)
        job = store.state()["jobs"][claim["job_id"]]
        self.assertTrue(job.get("snapshot"))
        act = engine.next_action(store.goal(), store.state())
        self.assertEqual((act["kind"], act["task"]), ("work", "b"))      # not wait_job
        (self.root / "a.txt").unlink()                                   # live edits do not reach the check
        checks.wait_job(store, job["id"], 60)
        self.assertEqual(engine.task_by_id(store.state(), "a")["status"], "done")
        self.assertFalse(Path(job["snapshot"]).exists())                 # the copy is removed afterwards


class WorktreeGoalTests(ProjectCase):
    def side_goal(self):
        store = self.project.create_goal({"id": "side", "kind": "develop", "title": "Side", "objective": "add() must add",
                                          "acceptance": [{"id": "tests", "run": "python3 test_calc.py"}],
                                          "agent": {"adapter": "fake"}}, make_current=False)
        approve(store)
        return store

    def test_a_worktree_goal_runs_beside_the_main_tree_and_merges_when_done(self):
        from loop_engineering.store import try_lock
        store = self.side_goal()
        before = (self.root / "calc.py").read_text()
        main_lock = try_lock(self.project.loop / "runner.lock")      # the main runner is busy with another goal
        try:
            result = Runner(self.project, "side", log=lambda m: None, worktree=True).run()
        finally:
            main_lock.close()
        state = store.state()
        self.assertEqual(state["status"], "done", state["status_reason"])
        self.assertEqual(result["worktree"]["status"], "merged")
        self.assertIn("calc.py", result["worktree"]["files"])
        self.assertNotEqual((self.root / "calc.py").read_text(), before)   # applied to the main tree, uncommitted
        self.assertIn("return a + b", (self.root / "calc.py").read_text())
        self.assertFalse(Path(state["worktree"]["path"]).exists())        # the worktree is cleaned up
        prompt = next((store.runs).glob("turn-*/prompt.md")).read_text()
        self.assertIn("its own git worktree", prompt)
        out = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True)
        self.assertNotIn("worktrees", out.stdout)

    def test_snapshot_carries_uncommitted_work_and_conflicts_wait_for_a_person(self):
        from loop_engineering import worktree
        (self.root / "notes.txt").write_text("untracked work in the main tree\n")
        (self.root / ".gitignore").write_text("deps/\n")
        (self.root / "deps").mkdir()
        (self.root / "deps" / "lib.txt").write_text("dependency\n")
        self.side_goal()
        record = worktree.prepare(self.project, "side")
        side = Path(record["path"])
        self.assertTrue((side / "notes.txt").is_file())                  # uncommitted files come along
        if sys.platform == "darwin":
            self.assertTrue((side / "deps" / "lib.txt").is_file())     # ignored dependencies are cloned
        (side / "calc.py").write_text("def add(a, b):\n    return a + b  # side\n")
        (self.root / "calc.py").write_text("def add(a, b):\n    return b + a  # main\n")
        result = worktree.integrate(self.project, "side", record, self.project.loop / "side.patch")
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(Path(result["patch"]).is_file())
        self.assertIn("# main", (self.root / "calc.py").read_text())   # the main tree is left untouched
        self.assertTrue(side.exists())                                   # kept until a person resolves it
