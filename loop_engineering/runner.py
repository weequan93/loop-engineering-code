"""The autonomous runner: dispatch agent turns until the goal finishes or needs a human.

One runner per goal (an OS file lock). The runner never makes an "empty" model
call: while waiting on a human, a schedule or a paused flag it only polls files.
After every agent turn the controller itself measures progress (workspace
fingerprint and state changes) and enforces turn, time, cost and stagnation
limits. A crashed runner leaves its heartbeat behind; the next runner records
the interrupted turn and continues from the files (the workspace is the truth).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from . import adapters as adapters_mod
from . import checks as checks_mod
from . import engine, procs, prompts
from . import queue as queue_mod
from .store import Project, try_lock
from .util import LoopError, digest, now, write_json

POLL_SECONDS = 5
HEARTBEAT_SECONDS = 15
SUBSTANTIVE = ("plan.", "task.", "review.", "check.result", "job.started", "goal.", "question.", "note.evidence", "note.hypothesis",
               "note.root_cause", "job.finished")


class Runner:
    def __init__(self, project: Project, goal_id: str | None = None, *, adapter: str | None = None,
                 max_turns: int | None = None, sleep=time.sleep, clock=time.time, log=None):
        self.project = project
        self.store = project.goal(goal_id)
        self.adapter_name = adapter
        self.max_turns = max_turns
        self.sleep, self.clock = sleep, clock
        self.turns_here = 0
        self.current_pgid = None
        self.stop_requested = False
        self.log = log or (lambda message: print(f"[loop {now()}] {message}", flush=True))
        self._beat = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> dict:
        """Drive the goal, then each approved goal queued after it (one working tree, one runner)."""
        project_lock = try_lock(self.project.loop / "runner.lock")
        if project_lock is None:
            raise LoopError("Another runner already drives this project (one goal at a time per working tree)")
        try:
            self._install_signals()
            completed = []
            while True:
                result = self._run_goal()
                completed.append(result)
                if result["status"] != "done" or self.stop_requested:
                    break
                if self.max_turns is not None and self.turns_here >= self.max_turns:
                    break
                following = queue_mod.next_after(self.project, self.store.id)
                if not following:
                    proposed = self._propose_next()
                    if proposed is not None:
                        result["proposed"] = proposed
                    break
                store = self.project.goal(following)
                if not queue_mod.approved(store):
                    self._announce_queue(store, False)
                    result["queue"] = f"Next queued goal {following} needs approval: loop approve --goal {following}"
                    break
                self._announce_queue(store, True)
                self.project.set_current(following)
                store.render()
                self.store = store
            final = completed[-1]
            if len(completed) > 1:
                final["goals"] = [{"goal": r["goal"], "status": r["status"]} for r in completed]
            return final
        finally:
            project_lock.close()

    def _propose_next(self):
        """Queue ran dry: let the planning agent draft the next goals (once per finished goal)."""
        from . import planner
        if not planner.settings(self.project)["propose_next"] or planner.already_proposed_after(
                self.project, self.store.id):
            return None
        try:
            entry = planner.propose(self.project, after_goal=self.store.id, log=self.log,
                                    cancelled=lambda: self.stop_requested)
        except LoopError as exc:
            self.log(f"planner skipped: {exc}")
            return None
        return entry["created"]

    def _announce_queue(self, store, starting: bool):
        from .notify import config, send
        title = store.goal()["title"]
        message = (f"Previous goal {self.store.id} is done; starting queued goal {store.id}." if starting else
                   f"Previous goal {self.store.id} is done; queued goal {store.id} waits for your approval.")
        self.log(message)
        if os.environ.get("LOOP_NOTIFY") == "0":
            return
        try:
            send(config(self.project.root), {"title": ("Next goal started · " if starting else "Approve next goal · ")
                                             + title, "message": message, "project": str(self.project.root),
                                             "command": "loop status" if starting else
                                             f"loop approve --goal {store.id} && loop start"})
        except Exception:
            pass

    def _run_goal(self) -> dict:
        handle = try_lock(self.store.dir / "runner.lock")
        if handle is None:
            raise LoopError(f"Another runner already drives goal {self.store.id}")
        previous = self.store.runner()
        self._beat = threading.Event()
        try:
            self._recover(previous)
            self._announce()
            heartbeat = threading.Thread(target=self._heartbeat, daemon=True, name="loop-heartbeat")
            heartbeat.start()
            return self._loop()
        finally:
            self._beat.set()
            info = self.store.runner() or {}
            info.update(exited_at=now(), turn=None)
            self.store.write_runner(info)
            self.store.render()
            handle.close()

    def _announce(self):
        pid = os.getpid()
        self.store.write_runner({"pid": pid, "identity": procs.identity(pid), "host": socket.gethostname(),
                                 "started_at": now(), "heartbeat_at": now(), "adapter": self._adapter_name(),
                                 "turn": None, "exited_at": None})

        def started(goal, state):
            if not state.get("started_at"):
                state["started_at"] = now()
            if state["status"] == "ready":
                state["status"], state["status_reason"] = "running", "Runner started."
        self.store.mutate("runner.started", started, pid=pid, adapter=self._adapter_name())

    def _recover(self, previous: dict | None):
        if not previous or previous.get("exited_at") or not previous.get("turn"):
            return
        turn = previous["turn"]
        if turn.get("pgid") and not procs.group_quiescent(turn["pgid"]) and procs.group_members(turn["pgid"]):
            raise LoopError(f"The previous runner's agent process group {turn['pgid']} is still alive. "
                            "Stop it (or wait), then start the runner again.")
        def interrupted(goal, state):
            state["turns"].append({"n": turn["n"], "action": turn["action"], "outcome": "interrupted",
                                   "started_at": turn["started_at"], "seconds": 0, "progress": False,
                                   "summary": "Runner stopped during this turn; the workspace was kept as-is."})
            state["turns"] = state["turns"][-50:]
            if goal["policy"]["pause_on_interrupt"]:
                state["control"] = "pause"
                state["status"], state["status_reason"] = "paused", (
                    "An agent turn was interrupted. Review its effects, then `loop resume`.")
        self.store.mutate("turn.interrupted", interrupted, turn=turn["n"])
        self.log(f"Recorded interrupted turn {turn['n']}")

    def _install_signals(self):
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, _frame):
            self.stop_requested = True
            if self.current_pgid:
                procs.terminate_group(self.current_pgid, grace=5)
        for signum in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
            signal.signal(signum, handler)

    def _heartbeat(self):
        beat, store = self._beat, self.store
        while not beat.wait(HEARTBEAT_SECONDS):
            info = store.runner()
            if info and not info.get("exited_at"):
                info["heartbeat_at"] = now()
                try:
                    store.write_runner(info)
                except OSError:
                    pass

    def _adapter_name(self) -> str:
        return self.adapter_name or self.store.goal()["agent"]["adapter"]

    # ------------------------------------------------------------------ main loop

    def _loop(self) -> dict:
        while True:
            if self.stop_requested:
                return self._exit("Runner received a stop signal.")
            goal, state = self.store.goal(), self.store.state()
            if state.get("control") == "pause":
                self.store.mutate("goal.paused", lambda g, s: s.update(status="paused",
                                                                       status_reason="Paused by an operator."))
                return self._exit("Paused.")
            if state.get("control") == "stop" or state["status"] == "stopped":
                return self._exit("Stopped.")
            stamp = checks_mod.fingerprint(self.project.root)
            act = engine.next_action(goal, state, stamp)
            self.log(f"next: {act['kind']} ({act['actor']}) — {act['summary'][:160]}")
            if act["actor"] == "none":
                if act["kind"] == "idle":
                    self._wait_until(act.get("until"))
                    continue
                return self._exit(act["summary"])
            if act["actor"] == "human":
                if act["kind"] in {"answer"} and not self._once():
                    self._wait_for_human(state)
                    continue
                return self._exit("Needs a human: " + act["summary"])
            if act["actor"] == "controller":
                if act["kind"] == "wait_job":
                    self._wait_job(act["job"])
                else:
                    self._controller_step(goal, act)
                continue
            problem = engine.budget_problem(goal, state)
            if problem:
                self.store.mutate("goal.limit", lambda g, s: s.update(status="limit", status_reason=problem +
                                  ". Raise the limit in goal.json (re-approve) or `loop resume` to grant a new budget."))
                return self._exit(problem)
            if self.max_turns is not None and self.turns_here >= self.max_turns:
                return self._exit(f"Stopped after {self.turns_here} turn(s) as requested.")
            self._agent_turn(goal, state, act, stamp)

    def _once(self) -> bool:
        return self.max_turns is not None

    def _controller_step(self, goal, act):
        self.log(f"controller: {act['kind']}")
        engine.verify_now(self.store, actor="controller", cancelled=lambda: self.stop_requested)
        state = self.store.state()
        self.log(f"→ {state['status']}: {state['status_reason']}")

    def _wait_job(self, job_id: str):
        """Poll a background check job without model calls; it marks lost jobs itself."""
        self.log(f"waiting for check job {job_id}")
        while not self.stop_requested:
            job = checks_mod.wait_job(self.store, job_id, 0)
            state = self.store.state()
            if job["status"] != "running" or state.get("control"):
                self.log(f"check job {job_id}: {job['status']}")
                return
            self.sleep(POLL_SECONDS)

    def _wait_for_human(self, state):
        marker = json.dumps([q["id"] for q in state["questions"] if q["answer"] is None])
        while not self.stop_requested:
            self.sleep(POLL_SECONDS)
            current = self.store.state()
            if (json.dumps([q["id"] for q in current["questions"] if q["answer"] is None]) != marker
                    or current.get("control") or current["blocker"]):
                return

    def _wait_until(self, until: str | None):
        from .util import parse_time
        target = parse_time(until) or self.clock()
        while not self.stop_requested and self.clock() < target:
            self.sleep(min(POLL_SECONDS, max(0.1, target - self.clock())))
            state = self.store.state()
            if state.get("control") or state["next_cycle_at"] != until:
                return

    def _exit(self, reason: str) -> dict:
        self.log("exit: " + reason)
        return {"goal": self.store.id, "status": self.store.state()["status"], "reason": reason,
                "turns": self.turns_here}

    # ------------------------------------------------------------------ agent turn

    def _agent_turn(self, goal, state, act, stamp_before):
        gate = engine.find_gate(goal, act.get("review")) if act["kind"] == "review" else None
        chosen = engine.profile_for(goal, state, act, self.project.root)  # role, adapter, model, effort
        adapter = adapters_mod.get(self.adapter_name or chosen["adapter"], self.project.root)
        unavailable = adapter.available()
        if unavailable:
            self.store.mutate("goal.blocked", lambda g, s: engine.block(
                g, s, "environment", f"Agent adapter {adapter.name} unavailable: {unavailable}",
                f"adapter={self._adapter_name()}", "Checked PATH and adapters.json", "runner"))
            return
        if act["kind"] == "work":
            task = engine.task_by_id(state, act["task"])
            if task["status"] == "pending":
                self.store.mutate("task.started", lambda g, s: engine.start_task(g, s, act["task"]), task=act["task"])
        n = state["iteration"] + 1
        workdir = self.store.runs / f"turn-{n:04d}"
        workdir.mkdir(parents=True, exist_ok=True)
        state = self.store.state()
        prompt = prompts.turn_prompt(goal, state, act, n, self.project.root)
        (workdir / "prompt.md").write_text(prompt, encoding="utf-8")
        session = None if goal["policy"]["fresh_session_each_turn"] else state.get("session_id")
        turn_goal = {**goal, "agent": {**goal["agent"], "model": chosen["model"], "effort": chosen["effort"]}}
        if goal["policy"]["max_cost_usd"]:
            remaining = max(0.01, goal["policy"]["max_cost_usd"] - state["usage"]["cost_usd"])
            turn_goal = {**turn_goal, "policy": {**goal["policy"], "max_cost_usd": remaining}}
        mcp_args, token = None, None
        if gate:
            # A one-time token lets only this read-only reviewer session record the verdict.
            token = secrets.token_hex(16)
            mcp_args = ["--review-token", token]
            session = None
        turn = adapter.build(prompt, self.project.root, workdir, turn_goal, session, mcp_args=mcp_args,
                             readonly=bool(gate))
        write_json(workdir / "command.json", {"argv": [a if a != token else "<review-token>" for a in turn.argv],
                                              "env_keys": sorted(turn.env)})
        seq_before = (self.store.last_event() or {}).get("seq", 0)

        def reserve(goal, state):
            state["iteration"] = n
            state["status"], state["status_reason"] = "running", f"Turn {n}: {act['kind']} — {act['summary'][:160]}"
            if act.get("incident"):
                incident = next(i for i in state["incidents"] if i["id"] == act["incident"])
                incident["turns_used"] = incident.get("turns_used", 0) + 1
            state["review_turn"] = ({"review": gate["id"], "token": digest(token), "fingerprint": stamp_before,
                                     "turn": n, "recorded": False} if gate else None)
        self.store.mutate("turn.started", reserve, turn=n, action=act["kind"], adapter=adapter.name)
        started = now()
        self._set_turn({"n": n, "action": act["kind"], "started_at": started, "pgid": None})
        self.log(f"turn {n}: {act['kind']} via {adapter.name}")

        def on_start(pid):
            self.current_pgid = pid
            self._set_turn({"n": n, "action": act["kind"], "started_at": started, "pgid": pid})

        def cancelled():
            if self.stop_requested:
                return True
            try:
                return self.store.state().get("control") == "stop"
            except (OSError, ValueError):
                return False
        timeout = ((gate or {}).get("timeout_minutes") or goal["policy"]["turn_timeout_minutes"]) * 60
        result = procs.run(turn.argv, cwd=self.project.root, logs=workdir, timeout=timeout, stdin_text=turn.stdin,
                           env={**turn.env, "LOOP_AUTONOMOUS": "1"}, cancelled=cancelled, on_start=on_start,
                           poll=0.5)
        self.current_pgid = None
        self._set_turn(None)
        report = adapter.parse(result.stdout, result.stderr, workdir)
        stamp_after = checks_mod.fingerprint(self.project.root)
        events = [e for e in self.store.events() if e["seq"] > seq_before]
        substantive = [e["type"] for e in events if e["type"].startswith(SUBSTANTIVE)
                       and e["type"] not in {"turn.started", "task.started"}]
        progress = stamp_after != stamp_before or bool(substantive)
        outcome = result.outcome if result.outcome != "completed" else (
            "ok" if result.exit_code == 0 and not report.error else f"exit {result.exit_code}")
        self.turns_here += 1

        def collect(goal, state):
            usage = state["usage"]
            usage["turns"] += 1
            usage["agent_seconds"] += result.duration_ms / 1000
            if report.cost_usd is not None:
                usage["cost_usd"] = round(usage["cost_usd"] + report.cost_usd, 6)
            else:
                usage["cost_complete"] = False
            usage["input_tokens"] += report.input_tokens or 0
            usage["output_tokens"] += report.output_tokens or 0
            if report.session_id:
                state["session_id"] = report.session_id
            state["turns"].append({"n": n, "action": act["kind"], "task": act.get("task"), "adapter": adapter.name,
                                   "model": chosen["model"], "effort": chosen["effort"], "stage": chosen["stage"],
                                   "role": chosen["role"],
                                   "outcome": outcome, "started_at": started, "finished_at": now(),
                                   "seconds": result.duration_ms / 1000, "progress": progress,
                                   "events": substantive[:20], "cost_usd": report.cost_usd,
                                   "summary": (report.error or report.summary or "")[:2000],
                                   "log": str(workdir.relative_to(self.project.root))})
            state["turns"] = state["turns"][-50:]
            state["stagnant_turns"] = 0 if progress else state.get("stagnant_turns", 0) + 1
            if gate:
                recorded = (state.get("review_turn") or {}).get("recorded")
                state["review_turn"] = None
                if stamp_after != stamp_before:
                    state["notes"].append({"at": now(), "kind": "risk", "by": "runner",
                                           "text": f"Reviewer {gate['id']} changed the workspace; its verdict "
                                                   "does not apply to the new tree."})
                if not recorded:
                    state["turns"][-1]["summary"] = "Reviewer recorded no verdict. " + state["turns"][-1]["summary"]
            if act["kind"] == "remediate":
                incident = next((i for i in state["incidents"] if i["id"] == act["incident"]), None)
                if incident and incident["status"] == "open":
                    incident["needs_recheck"] = True
            if state["status"] == "running":
                state["status_reason"] = f"Turn {n} finished ({outcome}{'' if progress else ', no progress'})."
            if state["stagnant_turns"] >= goal["policy"]["stagnation_limit"] and not state["blocker"]:
                engine.block(goal, state, "other", f"{state['stagnant_turns']} consecutive turns made no observable "
                             "progress (no workspace or plan change).", _tail(result), "Repeated turns", "runner")
            if result.outcome == "unavailable":
                engine.block(goal, state, "environment", f"Could not launch the agent: {turn.argv[0]}",
                             _tail(result), "Launching the adapter command", "runner")
        self.store.mutate("turn.finished", collect, turn=n, outcome=outcome, progress=progress)
        self.log(f"turn {n} finished: {outcome}, progress={progress}")

    def _set_turn(self, turn):
        info = self.store.runner() or {}
        info["turn"] = turn
        info["heartbeat_at"] = now()
        self.store.write_runner(info)


def _tail(result) -> str:
    from .util import read_tail
    return (read_tail(result.stderr, 3000) or read_tail(result.stdout, 3000) or f"outcome={result.outcome}")


def start_background(project: Project, goal_id: str | None, adapter: str | None) -> dict:
    store = project.goal(goal_id)
    runner = store.runner()
    if runner and not runner.get("exited_at") and procs.alive(runner.get("pid"), runner.get("identity")):
        return {"already_running": True, "pid": runner["pid"]}
    probe = try_lock(project.loop / "runner.lock")
    if probe is None:
        return {"already_running": True, "note": "A runner is driving another goal of this project"}
    probe.close()
    argv = [sys.executable, "-m", "loop_engineering", "run", "--project", str(project.root), "--goal", store.id]
    if adapter:
        argv += ["--adapter", adapter]
    env = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    log = store.runs / "runner.log"
    pid = procs.spawn_detached(argv, cwd=project.root, log=log, env=env)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        info = store.runner()
        if info and info.get("pid") == pid:
            break
        time.sleep(0.1)
    return {"started": True, "pid": pid, "log": str(log.relative_to(project.root)),
            "watch": "loop status --watch   or   loop dashboard"}
