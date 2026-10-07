"""Controller-run checks, workspace fingerprints and background check jobs.

A check result is evidence only for the fingerprint it ran against. Agents can
ask for checks (``loop_check``) but the controller executes them and records
exit codes and log tails; a model's claim never substitutes for a check.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from . import procs
from .util import LoopError, new_id, now, read_tail

# Framework-owned paths never change the candidate (v1 invalidated running work
# every time it upgraded its own skill file inside the project).
FRAMEWORK_PATHS = (".loop/", ".claude/skills/loop/", ".agents/skills/loop/", ".mcp.json",
                   ".codex/config.toml", ".claude/settings.local.json")
SKIP_DIRS = {".git", ".loop", "node_modules", ".venv", "venv", "__pycache__", ".build", "build", "dist",
             "target", ".next", ".gradle", "DerivedData", ".pytest_cache", ".mypy_cache"}


def _framework(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in FRAMEWORK_PATHS)


def fingerprint(root: Path) -> str:
    """Cheap content fingerprint of the working tree, excluding framework files."""
    h = hashlib.sha256()
    if (root / ".git").exists():
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                                  timeout=30).stdout.strip()
            listing = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                     cwd=root, capture_output=True, timeout=60)
            if listing.returncode == 0:
                h.update(head.encode())
                entries = [e for e in listing.stdout.decode("utf-8", "replace").split("\0") if e]
                for entry in sorted(entries):
                    path = entry[3:]
                    if _framework(path):
                        continue
                    h.update(entry.encode())
                    target = root / path
                    if target.is_file() and target.stat().st_size <= 64 << 20:
                        h.update(hashlib.sha256(target.read_bytes()).digest())
                return "git:" + h.hexdigest()[:24]
        except (OSError, subprocess.SubprocessError):
            pass
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if _framework(rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            h.update(f"{rel}\0{stat.st_size}\0{int(stat.st_mtime_ns)}\0".encode())
            count += 1
            if count > 200000:
                return "walk:" + h.hexdigest()[:24] + ":truncated"
    return "walk:" + h.hexdigest()[:24]


def run_check(root: Path, check: dict, logs: Path, default_timeout: int,
              cancelled=lambda: False) -> dict:
    cwd = (root / check.get("cwd", ".")).resolve()
    if root not in [cwd, *cwd.parents]:
        raise LoopError(f"Check {check['id']} cwd escapes the project")
    timeout = check.get("timeout") or default_timeout
    started = now()
    if "run" in check:
        argv, shell = check["run"], True
    else:
        argv, shell = check["argv"], False
    result = procs.run(argv, cwd=cwd, logs=logs, timeout=timeout, env=check.get("env"), shell=shell,
                       cancelled=cancelled)
    if result.outcome == "completed":
        status = "pass" if result.exit_code == check.get("expect_exit", 0) else "fail"
    else:
        status = {"timeout": "timeout", "cancelled": "cancelled"}.get(result.outcome, "error")
    output = read_tail(result.stdout, 2500)
    errors = read_tail(result.stderr, 2500)
    return {"status": status, "exit_code": result.exit_code, "outcome": result.outcome,
            "duration_ms": result.duration_ms, "started_at": started, "finished_at": now(),
            "log": str(logs.relative_to(root)) if root in logs.parents else str(logs),
            "tail": (output + ("\n[stderr]\n" + errors if errors.strip() else ""))[-4000:],
            "cleanup": result.cleanup}


def check_index(goal: dict, state: dict) -> dict[str, dict]:
    """All runnable checks by qualified id: acceptance.<id> and <task>.<id>."""
    index = {f"acceptance.{c['id']}": c for c in goal["acceptance"]}
    for task in state["tasks"]:
        for c in task.get("checks", []):
            index[f"{task['id']}.{c['id']}"] = c
    return index


def select(goal: dict, state: dict, scope: str | None, ids: list[str] | None) -> list[str]:
    index = check_index(goal, state)
    if ids:
        missing = [i for i in ids if i not in index]
        if missing:
            raise LoopError(f"Unknown checks {missing}. Known: {sorted(index)}")
        return list(ids)
    if not scope or scope == "acceptance":
        from .model import TEST_STAGES
        order = {name: i for i, name in enumerate(TEST_STAGES)}
        chosen = sorted((k for k in index if k.startswith("acceptance.")),  # unit → … → acceptance
                        key=lambda k: order.get(index[k].get("stage", "acceptance"), len(order)))
    elif scope == "all":
        chosen = list(index)
    else:
        chosen = [k for k in index if k.startswith(scope + ".")]
    if not chosen:
        raise LoopError(f"No checks in scope {scope or 'acceptance'}")
    return chosen


def execute(store, qualified: list[str], *, actor: str, job_id: str | None = None,
            cancelled=lambda: False) -> dict:
    """Run checks now (in this process) and record each result. Returns {id: result}."""
    goal, state = store.goal(), store.state()
    index = check_index(goal, state)
    root = store.project.root
    stamp = fingerprint(root)
    results = {}
    batch = job_id or new_id("checks")
    for qualified_id in qualified:
        check = index[qualified_id]
        logs = store.runs / "checks" / batch / qualified_id
        result = run_check(root, check, logs, goal["policy"]["check_timeout_seconds"], cancelled)
        result["fingerprint"] = stamp
        result["actor"] = actor
        results[qualified_id] = result

        def record(_goal, state, qualified_id=qualified_id, result=result):
            state["checks"][qualified_id] = result
            return {"_event": {"check": qualified_id, "status": result["status"], "exit": result["exit_code"]}}
        store.mutate("check.result", record)
    after = fingerprint(root)
    if after != stamp:
        # Checks often write their own outputs. Bind results to the tree they left
        # behind; the controller always re-runs acceptance itself before "done".
        def note(_goal, state):
            for qualified_id in results:
                state["checks"][qualified_id].update(fingerprint=after, workspace_changed=True)
        store.mutate("check.workspace_changed", note)
        for result in results.values():
            result.update(fingerprint=after, workspace_changed=True)
    return results


def current(state: dict, qualified: list[str], stamp: str) -> bool:
    return all(state["checks"].get(q, {}).get("status") == "pass" and
               state["checks"][q].get("fingerprint") == stamp for q in qualified)


# ---------------------------------------------------------------- background jobs


def start_job(store, qualified: list[str], actor: str, purpose: dict | None = None) -> str:
    """Run checks in a detached process so MCP calls never hold a long check.

    ``purpose`` lets the finished job apply its result: {"type": "task", "task": id}
    finalizes a task-done claim; {"type": "acceptance"} applies final verification.
    """
    job_id = new_id("job")

    def reserve(_goal, state):
        running = [j for j in state["jobs"].values() if j["status"] == "running"]
        for job in running:
            if not procs.alive(job.get("pid"), job.get("identity")):
                job.update(status="lost", finished_at=now(), error="check process exited without a result")
        if any(j["status"] == "running" for j in state["jobs"].values()):
            raise LoopError("Another check job is running; poll it with loop_check(job_id=...) first")
        state["jobs"][job_id] = {"id": job_id, "checks": qualified, "status": "running", "actor": actor,
                                 "started_at": now(), "pid": None, "identity": None, "purpose": purpose}
        # Keep the job table bounded.
        for old in sorted(state["jobs"], key=lambda k: state["jobs"][k]["started_at"])[:-20]:
            if state["jobs"][old]["status"] != "running":
                del state["jobs"][old]
    store.mutate("job.started", reserve, job=job_id, checks=qualified)
    argv = [sys.executable, "-m", "loop_engineering", "_job", "--project", str(store.project.root),
            "--goal", store.id, "--job", job_id]
    env = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    pid = procs.spawn_detached(argv, cwd=store.project.root, log=store.runs / "jobs" / f"{job_id}.log", env=env)
    time.sleep(0.05)

    def started(_goal, state):
        job = state["jobs"][job_id]
        if job["status"] == "running" and job["pid"] is None:
            job.update(pid=pid, identity=procs.identity(pid))
    store.mutate("job.pid", started, job=job_id)
    return job_id


def run_job(store, job_id: str) -> None:
    state = store.state()
    job = state["jobs"].get(job_id)
    if not job or job["status"] != "running":
        return

    def mark_self(_goal, state):
        state["jobs"][job_id].update(pid=os.getpid(), identity=procs.identity(os.getpid()))
    store.mutate("job.pid", mark_self, job=job_id)
    try:
        results = execute(store, job["checks"], actor=job["actor"], job_id=job_id)
        summary = {k: v["status"] for k, v in results.items()}
        purpose = job.get("purpose") or {}

        def finish(goal, state):
            from . import engine
            state["jobs"][job_id].update(status="finished", finished_at=now(), results=summary)
            if purpose.get("type") == "task":
                engine.apply_task_checks(goal, state, purpose["task"], results)
            elif purpose.get("type") == "acceptance":
                if goal["kind"] == "operate":
                    engine.apply_health(goal, state, results)
                else:
                    stamp = next(iter(results.values()))["fingerprint"]
                    engine.apply_verification(goal, state, results, stamp)
        store.mutate("job.finished", finish, job=job_id, results=summary, purpose=purpose.get("type"))
    except Exception as exc:
        def fail(_goal, state, exc=exc):
            state["jobs"][job_id].update(status="error", finished_at=now(), error=f"{type(exc).__name__}: {exc}")
        store.mutate("job.error", fail, job=job_id)
        raise


def wait_job(store, job_id: str, seconds: float) -> dict:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        state = store.state()
        job = state["jobs"].get(job_id)
        if not job:
            raise LoopError(f"Unknown job {job_id}")
        if job["status"] != "running":
            return job
        if job.get("pid") and not procs.alive(job["pid"], job.get("identity")):
            time.sleep(0.3)  # the job may have just committed its result
            job = store.state()["jobs"][job_id]
            if job["status"] == "running":
                def lost(_goal, state):
                    state["jobs"][job_id].update(status="lost", finished_at=now(),
                                                 error="check process exited without a result")
                store.mutate("job.lost", lost, job=job_id)
                return store.state()["jobs"][job_id]
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(0.25)
