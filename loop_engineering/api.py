"""One service layer shared by the MCP server, the CLI and the runner."""

from __future__ import annotations

from copy import deepcopy

from . import checks as checks_mod
from . import engine, model, prompts, status
from .store import Project
from .util import LoopError, digest, read_json, write_json


class Service:
    def __init__(self, project: Project, *, actor: str = "agent", interactive: bool = True,
                 review_token: str | None = None, planner_token: str | None = None):
        self.project, self.actor, self.interactive = project, actor, interactive
        self.review_token, self.planner_token = review_token, planner_token

    def _store(self, goal_id=None):
        return self.project.goal(goal_id)

    # ---------------------------------------------------------------- reading

    def status(self, goal_id=None, verbose=False) -> dict:
        store = self._store(goal_id)
        result = status.summary(store)
        result["goals"] = self.project.goal_ids()
        from . import queue as queue_mod
        result["queue"] = queue_mod.view(self.project)
        if verbose:
            result["markdown"] = status.markdown(store)
        problems = store.verify() if verbose else []
        if problems:
            result["integrity_problems"] = problems
        return result

    def next(self, goal_id=None) -> dict:
        store = self._store(goal_id)
        goal, state = store.goal(), store.state()
        act = engine.next_action(goal, state, checks_mod.fingerprint(self.project.root))
        if act["actor"] == "agent" and act["kind"] == "work":
            task = engine.task_by_id(state, act["task"])
            if task["status"] == "pending":
                store.mutate("task.started", lambda g, s: engine.start_task(g, s, act["task"]), task=act["task"])
                state = store.state()
        result = {"action": act}
        if act["kind"] == "review" and not self.review_token:
            result["assignment"] = ("An independent review is next. It must not be done by the agent that did the "
                                    "work: the runner dispatches a separate read-only reviewer (`loop run "
                                    "--max-turns 1`), or the user records one with `loop review <id> pass|fail`.")
        elif act["actor"] == "agent":
            result["assignment"] = prompts.assignment(goal, state, act, self.project.root)
            result["context"] = prompts.context_block(goal, state)
        elif act["actor"] == "controller":
            result["assignment"] = ("This step belongs to the controller. In an autonomous run the runner does it. "
                                    "In a chat, call loop_check(scope='acceptance') to run it now.")
        elif act["actor"] == "human":
            result["assignment"] = "Waiting on a person: " + act["summary"]
        return result

    # ---------------------------------------------------------------- goal intake

    def goal_draft(self, values: dict, goal_id=None, enqueue: bool = False) -> dict:
        """Create/update a draft. enqueue=True appends it to the project queue without
        switching the current goal (draft the next increments while one runs)."""
        values = dict(values)
        enqueue = bool(values.pop("enqueue", enqueue))
        if values.get("kind", "develop") == "develop" and "pipeline" not in values and not (
                goal_id or values.get("id")) in self.project.goal_ids():
            values["pipeline"] = "full"  # new develop goals run the full pipeline unless told otherwise
        if self.planner_token:
            return self._planner_draft(values)
        self._interactive_only("draft goals")
        target = goal_id or values.get("id")
        if target and target in self.project.goal_ids():
            store = self._store(target)
            current = store.raw_goal()
            merged = {**current, **{k: v for k, v in values.items() if v is not None}, "id": current["id"]}
            data = model.goal(merged)
            write_json(store.goal_path, data)

            def touched(_goal, state):
                if state["approved_digest"] and state["approved_digest"] != model.contract_digest(data):
                    state["status_reason"] = "Contract changed; it needs approval again."
            store.mutate("goal.updated", touched)
            if not enqueue:
                self.project.set_current(store.id)
        else:
            store = self.project.create_goal(values, make_current=not enqueue)
        if enqueue:
            from . import queue as queue_mod
            queue_mod.add(self.project, [store.id])
        goal = store.goal()
        return {"goal": goal, "problems": model.approval_problems(goal), "path": str(store.goal_path),
                "next": "Show the user this goal (objective, acceptance, policy). Approve only after they confirm."}

    def _planner_draft(self, values: dict) -> dict:
        """Planning agent: new queued drafts only (or revisions of its own drafts); never approves."""
        from . import planner, queue as queue_mod
        session = planner.check_token(self.project, self.planner_token)
        goal_id = values.get("id")
        if goal_id in self.project.goal_ids():
            if goal_id not in session.get("created", []):
                raise LoopError(f"Goal {goal_id} already exists; the planner only creates new goals")
            store = self.project.goal(goal_id)
            data = model.goal({**store.raw_goal(), **{k: v for k, v in values.items() if v is not None}, "id": goal_id})
            write_json(store.goal_path, data)
            store.mutate("goal.updated", lambda g, s: None, by="planner")
        else:
            model.goal(values)  # validate before counting against the session limit
            if not goal_id:
                values["id"] = model.goal(values)["id"]
            planner.record_draft(self.project, values["id"])
            store = self.project.create_goal(values, make_current=False)

            def mark(_goal, state):
                state["status_reason"] = "Proposed by the planning agent. Review goal.json, then `loop approve --goal " \
                                         f"{store.id}`."
                state["notes"].append({"at": state["updated_at"], "kind": "decision", "by": "planner",
                                       "text": "Drafted by the planning agent; not approved."})
            store.mutate("goal.proposed", mark)
        queue_mod.add(self.project, [store.id])
        return {"goal": store.goal(), "problems": model.approval_problems(store.goal()), "path": str(store.goal_path),
                "queued": True, "next": "Draft the next goal, or finish with a summary. A human approves."}

    def goal_approve(self, confirmed_by_user: bool, goal_id=None, by=None) -> dict:
        self._interactive_only("approve goals")
        if confirmed_by_user is not True:
            raise LoopError("Approval needs the user's explicit confirmation in this conversation (confirmed_by_user=true)")
        store = self._store(goal_id)
        base = _git_head(self.project.root)

        def change(goal, state):
            result = engine.approve(goal, state, by or self.actor)
            state.setdefault("base_ref", base)
            return result
        store.mutate("goal.approved", change)
        return self.status(store.id)

    # ---------------------------------------------------------------- plan and tasks

    def plan(self, tasks=None, drop=None, goal_id=None, final=False) -> dict:
        store = self._store(goal_id)
        store.mutate("plan.updated", lambda g, s: engine.plan(g, s, tasks, drop, bool(final)))
        state = store.state()
        return {"tasks": [{k: t[k] for k in ("id", "title", "status", "depends_on")} | {
            "covers": t.get("covers", []), "checks": [c["id"] for c in t["checks"]]} for t in state["tasks"]],
            "uncovered_requirements": engine.uncovered(state) if engine.pipeline(store.goal()) else [],
            "plan_final": bool(state.get("plan_final"))}

    def requirements(self, items=None, drop=None, final=False, goal_id=None) -> dict:
        store = self._store(goal_id)
        if not engine.pipeline(store.goal()):
            raise LoopError("This goal does not use the full pipeline (goal.json pipeline: \"full\")")
        store.mutate("requirements.updated", lambda g, s: engine.set_requirements(g, s, items, drop, bool(final)))
        state = store.state()
        return {"requirements": len(state["requirements"]), "final": bool(state.get("requirements_final")),
                "ids": [r["id"] for r in state["requirements"]]}

    def task(self, action: str, task_id: str, summary: str | None = None, wait_seconds: float = 45,
             goal_id=None, reason=None, evidence=None, attempted=None, category="other") -> dict:
        store = self._store(goal_id)
        if action == "start":
            store.mutate("task.started", lambda g, s: engine.start_task(g, s, task_id), task=task_id)
            return {"task": self._task_view(store, task_id)}
        if action == "done":
            result = store.mutate("task.claimed", lambda g, s: engine.claim_done(g, s, task_id, summary or ""),
                                  task=task_id)
            if not result["needs_checks"]:
                return {"task": self._task_view(store, task_id), "verified": False,
                        "note": "No task checks; accepted. Final acceptance still applies."}
            job_id = checks_mod.start_job(store, result["checks"], self.actor, {"type": "task", "task": task_id})
            job = checks_mod.wait_job(store, job_id, wait_seconds)
            return self._job_view(store, job) | {"task": self._task_view(store, task_id)}
        if action == "block":
            store.mutate("goal.blocked", lambda g, s: engine.block(g, s, category, reason or summary or "",
                                                                    evidence or "", attempted or "", self.actor))
            return self.status(store.id)
        raise LoopError("action must be start, done or block")

    def _task_view(self, store, task_id):
        task = engine.task_by_id(store.state(), task_id)
        view = {k: task.get(k) for k in ("id", "title", "status", "attempts", "summary")}
        if task.get("last_failure") and task["status"] != "done":
            view["last_failure"] = task["last_failure"]
        return view

    # ---------------------------------------------------------------- checks

    def check(self, scope=None, ids=None, job_id=None, wait_seconds: float = 45, goal_id=None) -> dict:
        store = self._store(goal_id)
        if job_id:
            return self._job_view(store, checks_mod.wait_job(store, job_id, wait_seconds))
        goal, state = store.goal(), store.state()
        qualified = checks_mod.select(goal, state, scope, ids)
        purpose = None
        act = engine.next_action(goal, state, checks_mod.fingerprint(self.project.root))
        if scope == "acceptance" and not ids and act["kind"] in {"verify", "health_check", "repair"}:
            purpose = {"type": "acceptance"}
        job_id = checks_mod.start_job(store, qualified, self.actor, purpose)
        return self._job_view(store, checks_mod.wait_job(store, job_id, wait_seconds))

    def _job_view(self, store, job) -> dict:
        state = store.state()
        view = {"job_id": job["id"], "status": job["status"], "checks": {}}
        for qualified in job["checks"]:
            result = state["checks"].get(qualified)
            if result and result.get("started_at", "") >= job["started_at"]:
                view["checks"][qualified] = {k: result.get(k) for k in ("status", "exit_code", "duration_ms", "tail")}
            else:
                view["checks"][qualified] = {"status": "pending" if job["status"] == "running" else "missing"}
        if job["status"] == "running":
            view["next"] = f"Still running. Poll with loop_check(job_id='{job['id']}')."
        if job.get("error"):
            view["error"] = job["error"]
        view["goal_status"] = state["status"]
        return view

    # ---------------------------------------------------------------- notes, questions, blocking

    def note(self, kind: str, text: str, task_id=None, finding_id=None, finding_status=None, goal_id=None) -> dict:
        store = self._store(goal_id)
        result = store.mutate("note." + kind, lambda g, s: engine.note(g, s, kind, text, task_id, finding_id,
                                                                     finding_status, self.actor))
        return {"recorded": True, **(result or {})}

    def ask(self, question, options=None, blocking=True, kind="question", action=None, goal_id=None) -> dict:
        store = self._store(goal_id)
        result = store.mutate("question.asked", lambda g, s: engine.ask(g, s, question, options, blocking, kind,
                                                                        action, self.actor))
        return {"question": result["question"],
                "next": "Stop working on anything that depends on this answer. The human answers with "
                        f"`loop answer {result['question']['id']} ...`."}

    def answer(self, question_id: str, reply: str, goal_id=None, by=None) -> dict:
        self._interactive_only("answer questions")
        store = self._store(goal_id)
        store.mutate("question.answered", lambda g, s: engine.answer(g, s, question_id, reply, by or self.actor))
        return self.status(store.id)

    def block(self, category, reason, evidence="", attempted="", goal_id=None) -> dict:
        store = self._store(goal_id)
        store.mutate("goal.blocked", lambda g, s: engine.block(g, s, category, reason, evidence, attempted,
                                                               self.actor))
        return self.status(store.id)

    def unblock(self, note: str, goal_id=None, by=None) -> dict:
        self._interactive_only("unblock goals")
        store = self._store(goal_id)
        store.mutate("goal.unblocked", lambda g, s: engine.unblock(g, s, note, by or self.actor))
        return self.status(store.id)

    def finish(self, report: str, wait_seconds: float = 45, goal_id=None) -> dict:
        store = self._store(goal_id)
        stamp = checks_mod.fingerprint(self.project.root)
        result = store.mutate("goal.finish_claimed", lambda g, s: engine.finish(g, s, report, stamp))
        if not result["verify"]:
            return self.status(store.id)
        goal = store.goal()
        job_id = checks_mod.start_job(store, checks_mod.select(goal, store.state(), "acceptance", None),
                                      self.actor, {"type": "acceptance"})
        return self._job_view(store, checks_mod.wait_job(store, job_id, wait_seconds))

    def stage_done(self, stage: str, path: str, summary: str = "", goal_id=None) -> dict:
        """A document stage (intake, solution, test_design, resources) delivered its file."""
        from .util import file_digest
        store = self._store(goal_id)
        target = (self.project.root / path).resolve()
        if self.project.root not in target.parents:
            raise LoopError("The stage document must be inside the project")
        if not target.is_file() or target.stat().st_size < 40:
            raise LoopError(f"{path} is missing or empty; write the document first")
        rel = str(target.relative_to(self.project.root))
        store.mutate("stage.done", lambda g, s: engine.complete_artifact(g, s, stage, rel, file_digest(target),
                                                                         summary), stage=stage)
        return self.next(goal_id)

    def review(self, review_id: str, verdict: str, findings: str = "", goal_id=None, back_to=None) -> dict:
        """Record a review verdict: the runner's reviewer turn (token) or a human at the CLI."""
        store = self._store(goal_id)
        stamp = checks_mod.fingerprint(self.project.root)
        if self.review_token:
            def change(goal, state):
                turn = state.get("review_turn") or {}
                if turn.get("token") != digest(self.review_token) or turn.get("review") != review_id:
                    raise LoopError("This session is not the reviewer for that review")
                if turn["fingerprint"] != stamp:
                    raise LoopError("The workspace changed during review; reviewers must not modify files")
                state["review_turn"]["recorded"] = True
                return engine.record_review(goal, state, review_id, verdict, findings, stamp, "reviewer", back_to)
        else:
            self._interactive_only("record reviews (independent reviewer turns do)")

            def change(goal, state):
                return engine.record_review(goal, state, review_id, verdict, findings, stamp, self.actor, back_to)
        store.mutate("review.recorded", change, review=review_id)
        return self.status(store.id)

    def control(self, command: str, goal_id=None, by=None) -> dict:
        store = self._store(goal_id)
        live = status.runner_view(store)["state"] == "running"
        store.mutate("goal." + command, lambda g, s: engine.control(g, s, command, by or self.actor, live))
        return self.status(store.id)

    def repair(self, note: str, goal_id=None, by=None) -> dict:
        """Accept an external state edit after a human inspected it (re-anchors the chain)."""
        self._interactive_only("repair state")
        store = self._store(goal_id)
        with store.lock():
            state = store.state()
            state["integrity"] = "ok"
            if state["status"] == "blocked" and not state["blocker"]:
                state["status"] = "ready"
            state["status_reason"] = f"State repaired by {by or self.actor}: {note}"
            store._commit(state, "integrity.repaired", {"note": note, "by": by or self.actor},
                          previous=store.last_event())
        return self.status(store.id)

    def _interactive_only(self, what: str) -> None:
        if not self.interactive:
            raise LoopError(f"Autonomous agents cannot {what}; a human does this with the loop CLI or in chat")

    def select(self, goal_id: str) -> dict:
        self._store(goal_id)
        self.project.set_current(goal_id)
        self._store(goal_id).render()
        return self.status(goal_id)


def _git_head(root) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=20)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def export(project: Project, goal_id=None) -> dict:
    store = project.goal(goal_id)
    return {"goal": read_json(store.goal_path), "state": deepcopy(store.state())}
