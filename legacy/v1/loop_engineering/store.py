"""Operator-owned SQLite events, state, and recovery journal outside the project."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, validate
from .authority import Authorities
from .native_contracts import validate_native


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.directory, 0o700)
        self.database = self.directory / "state.sqlite3"
        self.authorities = Authorities(self.directory / "authorities")
        for key_id, role in (("controller", "controller"), ("collector", "collector")):
            try:
                self.authorities.create(key_id, role)
            except FileExistsError:
                self.authorities.key(key_id)
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    run_id TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL,
                    PRIMARY KEY (run_id, digest)
                );
                CREATE TABLE IF NOT EXISTS authenticated_records (
                    run_id TEXT NOT NULL, digest TEXT NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY (run_id, digest)
                );
                CREATE TABLE IF NOT EXISTS replay (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    run_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, reason TEXT
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def run_dir(self, run_id: str) -> Path:
        if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in run_id):
            raise ContractError("Invalid run ID")
        path = self.directory / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create(self, data: dict) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO runs(id,data) VALUES(?,?)", (data["run_id"], json.dumps(data)))
            self._persist(connection, data, "task.accepted", {"contract_digest": canonical_digest(data["task"])})

    def get(self, run_id: str) -> dict:
        with self.connect() as connection:
            # One SQL statement reads a consistent projection/checkpoint pair
            # while another controller is appending a transaction.
            row = connection.execute("""SELECT data, (SELECT envelope FROM replay
                WHERE replay.run_id=runs.id ORDER BY sequence DESC LIMIT 1)
                FROM runs WHERE id=?""", (run_id,)).fetchone()
        if not row:
            raise ContractError("Run does not exist")
        data = strict_json_loads(row[0])
        if row[1] is None:
            raise ContractError("Run has no authenticated checkpoint; legacy runs require explicit migration to a new task")
        payload = self.authorities.verify(strict_json_loads(row[1]), "event", key_id="controller", role="controller")
        validate_native("replay_checkpoint", payload)
        if canonical_digest(data) != canonical_digest(payload["projection"]):
            raise ContractError("State projection differs from authenticated replay; inspect audit/rebuild")
        return data

    def runs(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM runs").fetchall()
        results = []
        for row in rows:
            data = self.get(row[0])
            results.append({"run_id": data["run_id"], "task_id": data["task"]["task_id"],
                            "workspace": data["workspace"], "status": data["state"]["status"],
                            "updated_at": data["state"]["updated_at"]})
        return sorted(results, key=lambda result: result["updated_at"], reverse=True)

    def save(self, data: dict, event_type: str, payload: dict | None = None) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._persist(connection, data, event_type, payload)

    def _persist(self, connection, data: dict, event_type: str, payload: dict | None) -> None:
        state = data["state"]
        row = connection.execute("SELECT data FROM runs WHERE id=?", (data["run_id"],)).fetchone()
        if row is None:
            raise ContractError("Run does not exist")
        current = strict_json_loads(row[0])
        if current["state"]["lease_token"] > state["lease_token"]:
            raise ContractError("A newer controller owns this run")
        if current["state"]["event_sequence"] != state["event_sequence"]:
            raise ContractError("State update is stale; reload the current projection")
        sequence = current["state"]["event_sequence"] + 1
        state.update(event_sequence=sequence, updated_at=utc_now())
        validate("state", state)
        event = {"schema_version": "0.2", "run_id": data["run_id"], "sequence": sequence,
                 "timestamp": state["updated_at"], "lease_token": state["lease_token"],
                 "event_type": event_type, "payload": payload or {}}
        previous = connection.execute("SELECT envelope FROM replay WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (data["run_id"],)).fetchone()
        previous_digest = canonical_digest(strict_json_loads(previous[0])) if previous else None
        checkpoint = {"schema_version": "1.0", "event": event, "projection": data, "previous_digest": previous_digest}
        validate_native("replay_checkpoint", checkpoint)
        envelope = self.authorities.sign("controller", "event", checkpoint)
        connection.execute("INSERT INTO events VALUES(?,?,?)", (data["run_id"], sequence, json.dumps(event)))
        connection.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), data["run_id"]))
        connection.execute("INSERT INTO replay VALUES(?,?,?)", (data["run_id"], sequence, json.dumps(envelope)))

    def cancelled(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT cancelled FROM runs WHERE id=?", (run_id,)).fetchone()
        return bool(row and row[0])

    def cancel(self, run_id: str) -> None:
        self.get(run_id)
        with self.connect() as connection:
            connection.execute("UPDATE runs SET cancelled=1 WHERE id=?", (run_id,))

    def pause(self, run_id: str, reason: str) -> None:
        self.get(run_id)
        if not reason.strip():
            raise ContractError("Pause requires a reason")
        with self.connect() as connection:
            connection.execute("INSERT INTO signals VALUES(?,1,?) ON CONFLICT(run_id) DO UPDATE SET paused=1,reason=excluded.reason", (run_id, reason))

    def paused(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT paused FROM signals WHERE run_id=?", (run_id,)).fetchone()
        return bool(row and row[0])

    def clear_pause(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE signals SET paused=0 WHERE run_id=?", (run_id,))

    def record(self, run_id: str, record: dict, *, envelope: dict | None = None) -> str:
        digest = canonical_digest(record)
        if envelope is None:
            envelope = self.authorities.sign("collector", "evidence", record)
        if self.authorities.verify(envelope, "evidence") != record:
            raise ContractError("Evidence signature covers a different record")
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?)", (run_id, digest, json.dumps(record)))
            connection.execute("INSERT OR IGNORE INTO authenticated_records VALUES(?,?,?)", (run_id, digest, json.dumps(envelope)))
        return digest

    def evidence(self, run_id: str, digests: list[str]) -> list[dict]:
        records = []
        with self.connect() as connection:
            for digest in digests:
                row = connection.execute("SELECT record FROM evidence WHERE run_id=? AND digest=?", (run_id, digest)).fetchone()
                if not row:
                    raise ContractError("Evidence record is unavailable")
                record = strict_json_loads(row[0])
                if canonical_digest(record) != digest:
                    raise ContractError("Evidence record was modified")
                signed = connection.execute("SELECT envelope FROM authenticated_records WHERE run_id=? AND digest=?", (run_id, digest)).fetchone()
                if not signed or self.authorities.verify(strict_json_loads(signed[0]), "evidence") != record:
                    raise ContractError("Evidence has no valid authenticated origin")
                records.append(record)
        return records

    def attestation(self, run_id: str, digest: str) -> dict:
        self.evidence(run_id, [digest])
        with self.connect() as connection:
            row = connection.execute("SELECT envelope FROM authenticated_records WHERE run_id=? AND digest=?", (run_id, digest)).fetchone()
        return strict_json_loads(row[0])

    def replay(self, run_id: str) -> dict:
        with self.connect() as connection:
            rows = connection.execute("SELECT sequence,envelope FROM replay WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
            events = connection.execute("SELECT sequence,event FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        if not rows:
            raise ContractError("Run has no authenticated replay history")
        if len(rows) != len(events) or rows[0][0] != 1:
            raise ContractError("Replay is incomplete or is a legacy run; no automatic reconstruction")
        previous = None
        for index, ((sequence, raw), (event_sequence, event_raw)) in enumerate(zip(rows, events), 1):
            envelope = strict_json_loads(raw)
            payload = self.authorities.verify(envelope, "event", key_id="controller", role="controller")
            validate_native("replay_checkpoint", payload)
            event, projection = payload["event"], payload["projection"]
            if (sequence != index or event_sequence != index or event != strict_json_loads(event_raw)
                    or event["sequence"] != index or event["run_id"] != run_id
                    or projection["run_id"] != run_id or projection["state"]["event_sequence"] != index
                    or event["lease_token"] != projection["state"]["lease_token"]
                    or payload["previous_digest"] != previous):
                raise ContractError("Replay sequence, event, identity, or hash chain is invalid")
            previous = canonical_digest(envelope)
        return projection

    def rebuild(self, run_id: str) -> dict:
        with self._lock(self.run_dir(run_id) / "writer.lock", "Another controller owns the run"):
            projection = self.replay(run_id)
            with self.connect() as connection:
                connection.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(projection), run_id))
            projection["state"]["lease_token"] += 1
            self.save(projection, "projection.rebuilt")
            return projection

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT event FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return [strict_json_loads(row[0]) for row in rows]

    @contextmanager
    def _lock(self, path: Path, message: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt
                    if path.stat().st_size == 0:
                        handle.write(b"0"); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as exc:
                raise ContractError(message) from exc
            try:
                yield
            finally:
                if os.name == "posix":
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    @contextmanager
    def workspace_writer(self, workspace: Path):
        information = Path(workspace).stat()
        workspace_key = canonical_digest(str(information.st_dev) + ":" + str(information.st_ino))[7:]
        # Coordinate this user's native/local writers even across state stores
        # and filesystem spelling aliases. Other OS owners remain an external
        # concurrency boundary; verification still checks actual candidate bytes.
        identity = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        shared = Path(tempfile.gettempdir()) / ("loop-workspace-locks-" + identity)
        if shared.is_symlink():
            raise ContractError("Shared workspace lock directory cannot be a symlink")
        shared.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(shared, 0o700)
        with self._lock(shared / workspace_key, "Another run is already operating this workspace"):
            yield

    @contextmanager
    def writer_group(self, run_ids, workspace):
        """Hold every affected run plus one shared workspace during a team journal.

        Locks follow the same run-before-workspace order as writer(). No child
        is claimed until every lock succeeds; callers still journal each save.
        """
        with ExitStack() as stack:
            for run_id in sorted(set(run_ids)):
                stack.enter_context(self._lock(self.run_dir(run_id) / "writer.lock",
                                              "Another controller is already operating this run"))
            stack.enter_context(self.workspace_writer(Path(workspace)))
            children = {run_id: self.get(run_id) for run_id in run_ids}
            if any(Path(child["workspace"]).resolve() != Path(workspace).resolve() for child in children.values()):
                raise ContractError("A grouped rework must use the same workspace")
            for child in children.values():
                child["state"]["lease_token"] += 1
                self.save(child, "controller.claimed")
            yield children

    @contextmanager
    def writer(self, run_id: str):
        workspace = self.get(run_id)["workspace"]
        with self._lock(self.run_dir(run_id) / "writer.lock", "Another controller is already operating this run"):
            with self.workspace_writer(Path(workspace)):
                data = self.get(run_id)
                data["state"]["lease_token"] += 1
                self.save(data, "controller.claimed")
                yield data
