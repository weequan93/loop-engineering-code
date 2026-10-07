"""Visible, file-based state inside the project's ``.loop`` directory.

Layout::

    .loop/
      STATUS.md              # current goal status, readable by any tool
      current                # id of the active goal
      goals/<id>/goal.json   # human contract (approved digest lives in state)
      goals/<id>/state.json  # controller state
      goals/<id>/events.jsonl# append-only, hash-chained history
      goals/<id>/STATUS.md
      goals/<id>/runner.json # runner heartbeat (not an event, rewritten often)
      goals/<id>/runs/       # turn and check logs (git-ignored)

Every mutation takes an exclusive lock, validates, writes atomically, appends
one event and re-renders the status files. Direct edits to state.json are
detected through the digest recorded in the last event.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from typing import Callable

from . import model
from .util import LoopError, atomic_write, digest, now, read_json, slug, write_json

LOOP_DIR = ".loop"
GITIGNORE = ("# Loop runtime files (logs, locks, heartbeats) and local notification secrets\n"
             "goals/*/runs/\ngoals/*/runner.json\ngoals/*/.lock\ngoals/*/notified.json\n*.lock\nnotify.json\n")

if os.name == "posix":
    import fcntl
else:  # pragma: no cover
    import msvcrt


@contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "posix":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise LoopError(f"Timed out waiting for lock {path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def try_lock(path: Path):
    """Non-blocking exclusive lock held for the life of the returned handle (runner ownership)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if os.name == "posix":
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


class Project:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise LoopError(f"Project directory does not exist: {self.root}")
        self.loop = self.root / LOOP_DIR

    @classmethod
    def find(cls, start: Path | str | None = None) -> "Project":
        here = Path(start or os.getcwd()).resolve()
        for candidate in [here, *here.parents]:
            if (candidate / LOOP_DIR / "goals").is_dir():
                return cls(candidate)
        return cls(here)

    def ensure(self) -> None:
        (self.loop / "goals").mkdir(parents=True, exist_ok=True)
        ignore = self.loop / ".gitignore"
        current = ignore.read_text() if ignore.exists() else ""
        missing = [line for line in GITIGNORE.splitlines() if line and not line.startswith("#")
                   and line not in current.splitlines()]
        if not ignore.exists() or missing:
            atomic_write(ignore, (current.rstrip("\n") + "\n" if current else GITIGNORE.split("\n")[0] + "\n")
                         + "".join(m + "\n" for m in missing))

    def goal_ids(self) -> list[str]:
        base = self.loop / "goals"
        if not base.is_dir():
            return []
        rows = []
        for path in base.iterdir():
            if (path / "goal.json").is_file():
                rows.append((path.stat().st_mtime, path.name))
        return [name for _, name in sorted(rows)]

    def current_id(self) -> str | None:
        try:
            value = (self.loop / "current").read_text().strip()
        except FileNotFoundError:
            value = ""
        if value and (self.loop / "goals" / value / "goal.json").is_file():
            return value
        ids = self.goal_ids()
        return ids[-1] if ids else None

    def set_current(self, goal_id: str) -> None:
        atomic_write(self.loop / "current", goal_id + "\n")

    def goal(self, goal_id: str | None = None) -> "GoalStore":
        goal_id = goal_id or self.current_id()
        if not goal_id:
            raise LoopError("No goal exists yet. Draft one with `loop goal new` or the loop_goal_draft tool.")
        store = GoalStore(self, slug(goal_id, "goal id"))
        if not store.goal_path.is_file():
            raise LoopError(f"Unknown goal {goal_id}")
        return store

    def create_goal(self, goal_values: dict, make_current: bool = True) -> "GoalStore":
        self.ensure()
        data = model.goal(goal_values)
        store = GoalStore(self, data["id"])
        if store.dir.exists():
            raise LoopError(f"Goal {data['id']} already exists")
        store.dir.mkdir(parents=True)
        write_json(store.goal_path, data)
        state = model.new_state(data)
        if make_current or not self.current_id():
            self.set_current(data["id"])
        with store.lock():
            store._commit(state, "goal.created", {"title": data["title"], "kind": data["kind"]}, previous=None)
        return store


class GoalStore:
    def __init__(self, project: Project, goal_id: str):
        self.project, self.id = project, goal_id
        self.dir = project.loop / "goals" / goal_id
        self.goal_path = self.dir / "goal.json"
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.status_path = self.dir / "STATUS.md"
        self.runner_path = self.dir / "runner.json"
        self.runs = self.dir / "runs"

    # ------------------------------------------------------------------ reading

    @contextmanager
    def lock(self):
        with file_lock(self.dir / ".lock"):
            yield

    def goal(self) -> dict:
        return model.goal(read_json(self.goal_path))

    def raw_goal(self) -> dict:
        return read_json(self.goal_path)

    def state(self) -> dict:
        return read_json(self.state_path)

    def last_event(self) -> dict | None:
        try:
            with self.events_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                lines = handle.read().splitlines()
        except FileNotFoundError:
            return None
        for line in reversed(lines):
            if line.strip():
                return json.loads(line)
        return None

    def events(self, limit: int | None = None) -> list[dict]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = [json.loads(line) for line in lines if line.strip()]
        return rows[-limit:] if limit else rows

    def verify(self) -> list[str]:
        """Full hash-chain audit plus a check that state.json matches the last event."""
        problems, previous = [], None
        for row in self.events():
            body = {k: v for k, v in row.items() if k != "hash"}
            if row.get("prev") != previous:
                problems.append(f"event {row.get('seq')} breaks the hash chain")
            if digest(body) != row.get("hash"):
                problems.append(f"event {row.get('seq')} was modified")
            previous = row.get("hash")
        last = self.last_event()
        if last and digest(self.state()) != last.get("state"):
            problems.append("state.json was edited outside the controller")
        return problems

    def runner(self) -> dict | None:
        try:
            return read_json(self.runner_path)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------ writing

    def mutate(self, event: str, change: Callable[[dict, dict], dict | None], **details):
        """Apply ``change(goal, state)`` under the lock; returns its return value."""
        with self.lock():
            state = self.state()
            last = self.last_event()
            if last and digest(state) != last.get("state") and state.get("integrity") == "ok":
                # Never silently adopt an external edit; record it and block.
                state["integrity"] = "state.json was edited outside the controller"
                state["status"], state["status_reason"] = "blocked", (
                    "state.json changed outside the controller. Inspect it, then run `loop repair`.")
                self._commit(state, "integrity.mismatch", {}, previous=last)
                raise LoopError(state["status_reason"])
            goal = self.goal()
            result = change(goal, state)
            payload = dict(details)
            if isinstance(result, dict) and "_event" in result:
                payload.update(result.pop("_event"))
            self._commit(state, event, payload, previous=last)
            return result

    def _commit(self, state: dict, event: str, payload: dict, previous: dict | None) -> None:
        state["updated_at"] = now()
        write_json(self.state_path, state)
        row = {"seq": (previous or {}).get("seq", 0) + 1, "at": state["updated_at"], "type": event,
               "data": payload, "state": digest(state), "prev": (previous or {}).get("hash")}
        row["hash"] = digest(row)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.render()
        self.notify(state)

    def notify(self, state: dict) -> None:
        from .notify import maybe_notify
        try:
            maybe_notify(self, self.goal(), state)
        except Exception:  # notifications are best effort
            pass

    def render(self) -> None:
        from .status import render_files
        try:
            render_files(self)
        except Exception as exc:  # status rendering must never break a state change
            atomic_write(self.status_path, f"# Loop status unavailable\n\n{type(exc).__name__}: {exc}\n")

    def write_runner(self, value: dict | None) -> None:
        if value is None:
            try:
                self.runner_path.unlink()
            except FileNotFoundError:
                pass
        else:
            write_json(self.runner_path, value)
