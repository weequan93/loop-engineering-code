"""A bounded, persistent native-host driver. No implicit provider dispatch.

Only an explicit CLI start runs this supervisor. It owns a dedicated Codex
session and resumes it after turn completion, while the controller still owns
task admission, original budgets, effects, evidence and completion.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, ROOT
from .native_project import inspect as inspect_project
from .native_supervisor_store import SupervisorStore
from .processes import execute, process_alive, process_group_alive, process_identity, same_process_identity
from .workspace import atomic_write


REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["checkpoint", "blocked"]},
        "summary": {"type": "string"},
        "blocker": {"type": ["string", "null"], "enum": [None, "human_input", "external_capability", "permission", "scope_change", "budget"]},
        "questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["status", "summary", "blocker", "questions"]}

WAIT_GATES = {"answer_questions", "awaiting_input", "await_evaluator", "await_evaluator_input",
              "missing_evaluator_authority"}
TERMINAL = {"COMPLETE", "CANCELLED", "LIMIT_REACHED", "RECOVERY_REQUIRED", "FAILED", "STALLED"}
MAX_FRAMEWORK_RECONNECTS = 3


def reconnect_supervisor(result, project, state_dir):
    """Replace the collected supervisor process, preserving its signed ledger."""
    if result.get("status") != "RECONNECT_REQUIRED" or result.get("pending") or result.get("process"):
        raise ContractError("Framework reconnect requires a collected supervisor with no pending host")
    argv = [sys.executable, str(ROOT / "scripts/loop.py"), "host-supervisor-run", str(project),
            "--state-dir", str(state_dir), "--supervisor-id", result["id"]]
    os.execv(sys.executable, argv)


def integer(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ContractError(f"{name} must be an integer from 1 to {maximum}")
    return value


def session_id(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise ContractError("Codex returned an invalid dedicated session ID") from exc
    return value


def read_report(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
        raise ContractError("Host did not return a bounded regular checkpoint report")
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"status", "summary", "blocker", "questions"}
            or not isinstance(value["status"], str) or value["status"] not in {"checkpoint", "blocked"}
            or not isinstance(value["summary"], str) or not value["summary"].strip() or len(value["summary"].encode()) > 8192
            or (value["blocker"] is not None and not isinstance(value["blocker"], str))
            or value["blocker"] not in REPORT_SCHEMA["properties"]["blocker"]["enum"]
            or not isinstance(value["questions"], list) or len(value["questions"]) > 16
            or any(not isinstance(q, str) or not q.strip() or len(q.encode()) > 2048 for q in value["questions"])
            or (value["status"] == "blocked") != (value["blocker"] is not None)):
        raise ContractError("Host checkpoint report is invalid")
    return value


def codex_events(path, expected_session=None):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16777216:
        raise ContractError("Host event stream is missing or too large")
    identities, completed, known, complete_usage = set(), 0, 0, True
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = strict_json_loads(line)
        if not isinstance(event, dict):
            raise ContractError("Host events must be JSON objects")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ContractError("Host events need a string type")
        if kind == "thread.started":
            identities.add(session_id(event.get("thread_id")))
        elif kind in {"turn.failed", "error"}:
            raise ContractError("Host reported a failed turn; preserve effects before recovery")
        elif kind == "turn.completed":
            completed += 1
            usage = event.get("usage", {})
            counts = [usage.get(k) for k in ("input_tokens", "output_tokens")] if isinstance(usage, dict) else []
            valid = len(counts) == 2 and all(type(n) is int and n >= 0 for n in counts)
            complete_usage = complete_usage and valid
            if valid:
                known += sum(counts)
    if len(identities) != 1 or completed != 1:
        raise ContractError("Host must identify one dedicated session and one completed turn")
    identity = identities.pop()
    if expected_session and expected_session != identity:
        raise ContractError("Host resumed a different session; do not continue it")
    return identity, known, complete_usage


class CodexSupervisorDriver:
    """Dedicated noninteractive session; never --last or an existing App chat."""
    def __init__(self, executable="codex", model=None):
        resolved = shutil.which(executable)
        if not resolved:
            raise ContractError("Codex CLI unavailable; use the native conversation supervised fallback")
        self.executable, self.model = resolved, model

    def preflight(self, approval_mode):
        from .native_supervisor_runtime import inspect_cli
        return inspect_cli(self, approval_mode)

    def command(self, service, data, artifacts, state):
        schema = artifacts / "report.schema.json"
        atomic_write(schema, (json.dumps(REPORT_SCHEMA) + "\n").encode(), 0o600)
        parent = service.team.store.directory / "host-workbenches"
        parent.mkdir(mode=0o700, exist_ok=True)
        args = [str(ROOT / "scripts/native_mcp.py"), "--project", str(service.project),
                "--state-dir", str(service.team.store.directory), "--workbench-parent", str(parent)]
        from .native_supervisor_runtime import mode
        approval_mode = mode(data["policy"].get("approval_mode", "never"))
        argv = [self.executable, "--no-daemon"]
        if approval_mode == "auto-review":
            # This global option selects workspace-write itself and conflicts
            # with explicit --sandbox. It also applies to exec resume.
            argv.append("--approve-for-me")
        else:
            argv += ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        # Explicit CLI overrides load the current backend without editing the
        # frozen project's skill/config. Retained drafts remain writable.
        writable = {str(artifacts), str(parent)}
        for row in state.get("worker_drafts", []) + state.get("dispatch_board", {}).get("workers", []):
            if row.get("directory") and row.get("current"):
                writable.add(str(Path(row["directory"]).resolve()))
        for directory in sorted(writable):
            argv.extend(["--add-dir", directory])
        for key, value in (("command", str(Path(sys.executable).absolute())), ("args", args),
                           ("enabled", True), ("required", True)):
            argv.extend(["-c", "mcp_servers.loop-native." + key + "=" + json.dumps(value, ensure_ascii=False)])
        argv += ["exec"] + (["resume", data["session_id"]] if data["session_id"] else [])
        argv += ["--json", "--output-schema", str(schema), "--output-last-message", str(artifacts / "report.json")]
        if self.model:
            argv += ["--model", self.model]
        return argv + ["-"]


class NativeSupervisor:
    def __init__(self, service, *, driver=None, executor=execute, clock=time.time, sleeper=time.sleep):
        self.service, self.store = service, service.team.store
        self.journal = SupervisorStore(self.store)
        self.driver, self.executor, self.clock, self.sleep = driver, executor, clock, sleeper

    def create(self, team_id, *, host_idle_confirmed, max_wall_seconds=28800, max_turns=100,
               turn_timeout_seconds=14400, poll_seconds=5, max_stagnant_turns=3, executable="codex", model=None,
               approval_mode="never"):
        from .native_supervisor_runtime import mode
        mode(approval_mode)
        if host_idle_confirmed is not True:
            raise ContractError("Stop existing project writers first and explicitly confirm --host-idle; stale heartbeats alone do not authorize takeover")
        if os.name != "posix":
            raise ContractError("Persistent native supervision currently requires POSIX owned process groups; keep the conversation fallback")
        team = self.service._data(team_id, native=True)
        root_team_id = team["native_host"].get("project_root") or team_id
        self.service._data(root_team_id, native=True)
        integer(max_wall_seconds, "Supervisor lifetime", 2592000)
        integer(max_turns, "Supervisor turns", 10000)
        integer(turn_timeout_seconds, "Host turn timeout", 86400)
        integer(poll_seconds, "Supervisor poll interval", 60)
        integer(max_stagnant_turns, "Consecutive unchanged turns", 20)
        if not isinstance(executable, str) or not executable.strip() or "\0" in executable:
            raise ContractError("A valid Codex executable is required")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 256):
            raise ContractError("A model override must be bounded text")
        if approval_mode == "auto-review":
            driver = self.driver or CodexSupervisorDriver(executable, model)
            driver.preflight(approval_mode)
        return self.journal.create({"project": str(self.service.project), "root_team_id": root_team_id,
            "team_id": team_id, "status": "READY", "reason": "Explicitly authorized dedicated host supervision",
            "policy": dict(max_wall_seconds=max_wall_seconds, max_turns=max_turns,
                turn_timeout_seconds=turn_timeout_seconds, poll_seconds=poll_seconds,
                max_stagnant_turns=max_stagnant_turns, executable=executable, model=model,
                approval_mode=approval_mode),
            "deadline_epoch": int(self.clock()) + max_wall_seconds, "session_id": None,
            "turns": 0, "dispatch_ms": 0, "known_tokens": 0, "usage_complete": True,
            "pending": None, "process": None, "signal": None, "stagnant_turns": 0,
            "waiting_stamp": None, "last_report": None, "host_idle_confirmed": True,
            "dashboard_url": None, "dashboard_path": None})

    def runtime_update(self, identity, **arguments):
        from .native_supervisor_runtime import update
        return update(self, identity, **arguments)

    def _data(self, identity):
        data = self.journal.get(identity)
        if data["project"] != str(self.service.project):
            raise ContractError("Host supervisor is outside the configured project")
        return data

    def status(self, identity):
        data = self._data(identity)
        owner = data.get("process")
        matched = bool(owner and owner["identity"] and same_process_identity(process_identity(owner["pid"]), owner["identity"])
                       and process_alive(owner["pid"]))
        return {**data, "process_observed": "running" if matched else "unknown_or_stopped",
            "remaining_wall_seconds": max(0, int(data["deadline_epoch"] - self.clock())),
            "host_cost": None,
            "assurance": "Supervisor checkpoint and local process observation; host reports are not acceptance evidence. CLI event tokens cover only reported turns, not all delegates, reviewers or product calls. Native OS authority remains supervised."}

    @contextmanager
    def _run_lock(self):
        info = self.service.project.stat()
        directory = Path(tempfile.gettempdir()) / ("loop-supervisor-locks-" + str(os.getuid()))
        if directory.is_symlink():
            raise ContractError("Supervisor lock directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        key = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()
        with self.store._lock(directory / key, "Another supervisor is already driving this project"):
            yield

    def control(self, identity, action):
        self._data(identity)
        if action not in {"pause", "cancel", "resume"}:
            raise ContractError("Unknown supervisor control")
        def change(data):
            if data["signal"] == "CANCELLED" or data["status"] == "CANCELLED":
                raise ContractError("Supervisor cancellation is terminal")
            if action == "resume":
                if data["pending"]:
                    raise ContractError("Inspect and reconcile the original dispatch before resuming")
                if data["status"] in {"COMPLETE", "CANCELLED", "LIMIT_REACHED"} or data["deadline_epoch"] <= self.clock():
                    raise ContractError("Supervisor cannot resume a terminal or expired ledger")
                data.update(signal=None, status="READY", reason="Explicit supervisor resume; original limits retained",
                            waiting_stamp=None, stagnant_turns=0)
            else:
                data["signal"] = "PAUSED" if action == "pause" else "CANCELLED"
                data["reason"] = "Explicit supervisor " + action + "; original team/checks remain intact"
                if not data["pending"]:
                    data["status"] = data["signal"]
        return self.journal.update(identity, "supervisor." + action, change)

    def reconcile(self, identity, note):
        if not isinstance(note, str) or not note.strip() or len(note.encode()) > 8192:
            raise ContractError("Actual effect inspection needs a bounded note")
        with self._run_lock():
            data = self._data(identity)
            pending = data["pending"]
            if not pending:
                raise ContractError("No unresolved original host dispatch")
            if pending.get("pid") and process_group_alive(pending["pid"]):
                raise ContractError("Original host process group is still alive or unobservable; do not replay it")
            def change(row):
                row["dispatch_ms"] += pending["reserved_ms"]
                row.update(pending=None, status="PAUSED" if row["signal"] == "PAUSED" else
                           "CANCELLED" if row["signal"] == "CANCELLED" else "READY",
                           reason="Original host effects inspected; lost dispatch charged conservatively",
                           reconciliation={"note": note, "dispatch": pending}, usage_complete=False,
                           prior_usage_complete=False)
                # A lost first response cannot supply a session identity. Start
                # a fresh dedicated context after actual effect reconciliation;
                # all original task/worker journals must still be recovered.
            return self.journal.update(identity, "supervisor.reconciled", change)

    def _state(self, data):
        root = self.service._data(data["root_team_id"], native=True)
        project = inspect_project(self.service, root)
        identity = project["latest_team_id"] if project else root["team_id"]
        self.service.watch_progress(identity)
        return self.service.progress(identity)

    def _stamp(self, state):
        data = self.service._data(state["team_id"], native=True)
        # Heartbeats and generated dashboards cannot manufacture progress.
        result = {"team_id": state["team_id"], "status": state["status"], "stage": state["stage"],
                  "questions": state["pending_questions"], "records": data["records"],
                  "candidate": self.service.team.snapshots.capture(self.service.project, self.service.team.profile(data))["digest"],
                  "checks": state["check_results"], "action": state["next_action"]["kind"],
                  "operations": [{k: v for k, v in row.items() if k != "elapsed_seconds"}
                                 for row in state.get("operations", [])], "drafts": [],
                  "workers": [{k: row.get(k) for k in ("id", "status", "agent_id", "current")}
                              for row in state.get("dispatch_board", {}).get("workers", [])]}
        for draft in state.get("worker_drafts", []):
            if draft.get("current") and draft.get("directory"):
                path = Path(draft["directory"])
                result["drafts"].append({"id": draft["id"], "status": draft["status"],
                    "candidate": self.service.team.snapshots.capture(path, self.service.team.profile(data))["digest"]})
        return canonical_digest(result)

    def _external_stop(self, data):
        latest = self.service._data(data["team_id"], native=True)
        root = self.service._data(data["root_team_id"], native=True)
        signals = {self.service.team.teams.signal(latest["team_id"]), self.service.team.teams.signal(root["team_id"]),
                   root["native_host"].get("project_signal")}
        return "CANCELLED" if "CANCELLED" in signals or latest["status"] == "CANCELLED" else (
            "PAUSED" if "PAUSED" in signals or latest["status"] == "PAUSED" else None)

    def _prompt(self, data, state):
        compact = {k: state.get(k) for k in ("team_id", "status", "stage", "task_counts", "next_action",
                   "pending_questions", "project_supervision")}
        return ("Use the current loop-engineering skill and loop-native MCP to continue this same authorized project. "
            "You are a dedicated supervisor-owned coordinator; the operator confirmed previous project writers stopped. "
            "Start with loop_handshake and loop_progress. Follow loop_next, inspect actual retained drafts and recover original journals; "
            "never reset usage, replace teams, loosen guards, manufacture acceptance, grant new paid resources or extend budgets. "
            "Read docs and collect material scope, environment, permissions, budget and evaluator decisions together during intake. "
            "Before a costly product check, run its bounded runtime/census/owned-cleanup preflight in the actual verification environment. "
            "A CLI-help probe is not execution evidence. Inspect retained process instances; never signal a historical group ID alone. "
            "When the operator enabled auto-review, request only the exact authorized boundary operation through official approval review; "
            "do not bypass sandboxing, change reviewer policies, ignore rules or weaken cleanup. Retain denials and complete independent work. "
            "Use loop_plan_preflight before freezing. Routine engineering choices already authorized need no repeated approval. "
            "Coordinate useful professional workers within registered limits; collect outputs and stop your owned writers before returning. "
            "Finish and collect background operations before a checkpoint; ending the CLI closes its MCP process. "
            "Continue implementation, repair, original checks, independent reviews and handoffs. Missing measurement producers/import paths are engineering. "
            "Actual human judgments remain pending. For a concrete external blocker, register the actual question if appropriate, prepare the procedure/recovery proposal, "
            "and return blocked with its real category; an audit or passing regressions alone is a checkpoint. "
            "Your final response is the checkpoint schema supplied by the driver. It does not certify acceptance. "
            "After a checkpoint this supervisor will resume this exact session using controller state; do not start nested CLIs or another supervisor.\n" +
            json.dumps(compact, ensure_ascii=False, separators=(",", ":")))

    def _finish(self, identity, status, reason, **extra):
        return self.journal.update(identity, "supervisor." + status.lower(),
                                   lambda row: row.update(status=status, reason=reason, **extra))

    def _remaining(self, data):
        return min(data["deadline_epoch"] - self.clock(), self.monotonic_deadline - time.monotonic())

    def run(self, identity):
        with self._run_lock():
            data = self._data(identity)
            if data["pending"]:
                return self._finish(identity, "RECOVERY_REQUIRED", "Original host dispatch unresolved; inspect effects before replay")
            if data["status"] in TERMINAL or data["signal"]:
                return data
            self.journal.update(identity, "supervisor.started", lambda row: row.update(
                process={"pid": os.getpid(), "identity": process_identity(os.getpid())}))
            self.monotonic_deadline = time.monotonic() + max(0, data["deadline_epoch"] - self.clock())
            try:
                return self._loop(identity)
            except (KeyboardInterrupt, SystemExit):
                self._finish(identity, "RECOVERY_REQUIRED", "Supervisor interrupted; inspect the original host dispatch")
                raise
            except (OSError, ValueError) as exc:
                return self._finish(identity, "RECOVERY_REQUIRED" if self._data(identity)["pending"] else "FAILED", str(exc)[:2048])
            finally:
                self.journal.update(identity, "supervisor.process_exited", lambda row: row.update(process=None))

    def _loop(self, identity):
        while True:
            data = self._data(identity)
            stop = data["signal"] or self._external_stop(data)
            if stop:
                return self._finish(identity, stop, "Actual supervisor/team/project stop retained")
            remaining = self._remaining(data)
            state = self._state(data)
            if data["team_id"] != state["team_id"] or data["dashboard_path"] != state.get("dashboard_path") or data["dashboard_url"] != state.get("dashboard_url"):
                data = self.journal.update(identity, "supervisor.observed", lambda row: row.update(
                    team_id=state["team_id"], dashboard_url=state.get("dashboard_url"), dashboard_path=state.get("dashboard_path")))
            action = state["next_action"]
            stamp = self._stamp(state)
            # State/snapshot preparation consumes real lifetime too.
            remaining = self._remaining(data)
            if state.get("evidence_problems"):
                return self._finish(identity, "BLOCKED", "Recorded evidence is missing or changed; original acceptance cannot be certified")
            if (action["kind"] in {"complete", "project_queue_complete"} and state["status"] == "COMPLETE"
                    and state["final_candidate_current"] and not state.get("evidence_problems")):
                return self._finish(identity, "COMPLETE", "Controller accepted the declared batch/queue contracts; unlisted scope remains unassessed")
            if remaining <= 0 or data["turns"] >= data["policy"]["max_turns"]:
                return self._finish(identity, "LIMIT_REACHED", "Original supervisor lifetime or dispatch bound exhausted")
            if action["kind"] == "reconnect_framework":
                if data.get("framework_reconnects", 0) >= MAX_FRAMEWORK_RECONNECTS:
                    return self._finish(identity, "BLOCKED", "Repeated framework changes exhausted automatic reconnects; inspect the deployment")
                compatibility = state.get("compatibility", {})
                return self.journal.update(identity, "supervisor.framework_reconnect", lambda row: row.update(
                    status="RECONNECT_REQUIRED", reason="Framework changed; reconnect the collected host with original ledger and limits",
                    framework_reconnects=row.get("framework_reconnects", 0) + 1,
                    framework_reconnect={key: compatibility.get(key) for key in ("loaded_fingerprint", "disk_fingerprint")},
                    waiting_stamp=None))
            if not action["continue_work"]:
                if action["kind"] in WAIT_GATES:
                    if data["status"] != "WAITING" or data["waiting_stamp"] != stamp or data["reason"] != action["summary"]:
                        self._finish(identity, "WAITING", action["summary"], waiting_stamp=stamp)
                    self.sleep(min(data["policy"]["poll_seconds"], remaining))
                    continue
                return self._finish(identity, "BLOCKED", action["summary"], waiting_stamp=stamp)
            if data["waiting_stamp"] == stamp:
                self.sleep(min(data["policy"]["poll_seconds"], remaining))
                continue  # Actual external input/changed state, not another empty model call.
            if data["stagnant_turns"] >= data["policy"]["max_stagnant_turns"]:
                return self._finish(identity, "STALLED", "Consecutive turns produced no controller/candidate progress; retain diagnostics for repair")
            try:
                driver = self.driver or CodexSupervisorDriver(data["policy"]["executable"], data["policy"]["model"])
                driver.preflight(data["policy"].get("approval_mode", "never"))
            except ContractError as exc:
                return self._finish(identity, "BLOCKED", "Runtime capability preflight: " + str(exc))
            timeout = min(data["policy"]["turn_timeout_seconds"], remaining)
            artifacts = self.store.run_dir(identity) / f"turn-{data['turns'] + 1}"
            artifacts.mkdir(mode=0o700)
            pending = {"number": data["turns"] + 1, "reserved_ms": math.ceil(timeout * 1000), "pid": None,
                       "identity": None, "artifacts": str(artifacts), "before": stamp}
            data = self.journal.update(identity, "supervisor.dispatch_reserved", lambda row: row.update(
                pending=pending, turns=row["turns"] + 1, status="RUNNING", waiting_stamp=None,
                prior_usage_complete=row["usage_complete"], usage_complete=False))
            argv = driver.command(self.service, data, artifacts, state)
            current = self._data(identity)
            stop = current["signal"] or self._external_stop(current)
            remaining = self._remaining(current)
            if stop or remaining <= 0:
                def abort(row):
                    row.update(status=stop or "LIMIT_REACHED", pending=None,
                               usage_complete=row["prior_usage_complete"],
                               last_aborted_reservation=pending,
                               reason="Stopped or exhausted during preparation; host was not dispatched")
                return self.journal.update(identity, "supervisor.dispatch_aborted", abort)
            timeout = min(timeout, remaining)
            def started(pid):
                self.journal.update(identity, "supervisor.host_started", lambda row: row["pending"].update(
                    pid=pid, identity=process_identity(pid)))
            def stopped():
                current = self._data(identity)
                return bool(current["signal"] or self.clock() >= current["deadline_epoch"] or
                            time.monotonic() >= self.monotonic_deadline or self._external_stop(current))
            result = self.executor(argv, self.service.project, artifacts / "logs", timeout=timeout,
                input_text=self._prompt(data, state), on_started=started, cancelled=stopped, max_output_bytes=16777216)
            if result.outcome != "completed" or result.exit_code != 0:
                # A killed host may have launched operations or changed files.
                # Keep pending until actual effect reconciliation, never blind retry.
                return self._finish(identity, "RECOVERY_REQUIRED", f"Host {result.outcome}, exit={result.exit_code}; original effects need inspection")
            returned_session, known, usage_complete = codex_events(result.stdout, data["session_id"])
            report = read_report(artifacts / "report.json")
            after_state = self._state(data)
            if any(row["status"] == "RUNNING" for row in after_state.get("operations", [])) or any(
                    row.get("pending_process") or row.get("pending_edit") or any(
                        attempt.get("status") == "RUNNING" for attempt in row.get("executors", {}).values())
                    for row in after_state["tasks"].values()):
                return self._finish(identity, "RECOVERY_REQUIRED", "Host ended with unresolved controller effects; inspect the original operation before another turn")
            after = self._stamp(after_state)
            def collected(row):
                row["known_tokens"] += known
                # Complete coverage is intentionally limited to owned CLI event
                # streams; it is never a total project/paid-cost assertion.
                row["usage_complete"] = usage_complete and row.get("prior_usage_complete", True)
                row["prior_usage_complete"] = row["usage_complete"]
                row["dispatch_ms"] += result.elapsed_ms
                row.update(pending=None, session_id=returned_session, last_report=report,
                           team_id=after_state["team_id"], status="WAITING" if report["status"] == "blocked" else "READY",
                           reason=report["summary"], waiting_stamp=after if report["status"] == "blocked" else None,
                           stagnant_turns=row["stagnant_turns"] + 1 if after == stamp else 0)
            self.journal.update(identity, "supervisor.host_collected", collected)

    def launch(self, identity):
        data = self._data(identity)
        if data["pending"] or data["signal"] or data["status"] in TERMINAL:
            raise ContractError("Inspect the original supervisor state before launching")
        owner = data.get("process")
        if owner and owner["identity"] and same_process_identity(process_identity(owner["pid"]), owner["identity"]) and process_alive(owner["pid"]):
            return {"supervisor_id": identity, "already_running": True, "launcher_pid": owner["pid"]}
        directory = self.store.run_dir(identity)
        argv = [sys.executable, str(ROOT / "scripts/loop.py"), "host-supervisor-run", str(self.service.project),
                "--state-dir", str(self.store.directory), "--supervisor-id", identity]
        with (directory / "supervisor.log").open("ab") as logs:
            process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=logs, stderr=logs, start_new_session=True)
        threading.Thread(target=process.wait, name="loop-supervisor-reaper", daemon=True).start()
        return {"supervisor_id": identity, "launcher_pid": process.pid, "log": str(directory / "supervisor.log"),
                "next": "Inspect host-supervisor-status and the Loop progress page. This process survives a chat turn/terminal closure, not machine shutdown."}
