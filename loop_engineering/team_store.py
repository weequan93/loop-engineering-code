"""Operator-owned, authenticated team checkpoints, separate from coding runs."""

from contextlib import contextmanager
import json
import time
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError
from .store import Store, utc_now


class TeamStore:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS team_events(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY(id, revision));
                CREATE TABLE IF NOT EXISTS team_signals(id TEXT PRIMARY KEY, signal TEXT NOT NULL);
            """)

    def _verify(self, raw: str, team_id: str) -> dict:
        payload = self.store.authorities.verify(strict_json_loads(raw), "event", key_id="controller", role="controller")
        if payload.get("kind") != "team-checkpoint" or payload["data"]["team_id"] != team_id:
            raise ContractError("Invalid team checkpoint identity")
        return payload

    def _read(self, connection, team_id: str) -> dict:
        row = connection.execute("""SELECT data, (SELECT envelope FROM team_events
            WHERE id=teams.id ORDER BY revision DESC LIMIT 1) FROM teams WHERE id=?""", (team_id,)).fetchone()
        if not row:
            raise ContractError("Team run does not exist")
        if not row[1] or strict_json_loads(row[0]) != self._verify(row[1], team_id)["data"]:
            raise ContractError("Team projection differs from authenticated checkpoint")
        return strict_json_loads(row[0])

    def get(self, team_id: str) -> dict:
        with self.store.connect() as connection:
            return self._read(connection, team_id)

    def create(self, data: dict) -> None:
        data.update(team_id="team-" + uuid4().hex, revision=0)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for (team_id,) in connection.execute("SELECT id FROM teams").fetchall():
                existing = self._read(connection, team_id)
                if existing["workspace"] == data["workspace"] and existing["status"] not in {"COMPLETE", "CANCELLED"}:
                    raise ContractError("An unfinished team already owns this workspace in this store")
            connection.execute("INSERT INTO teams VALUES(?,?)", (data["team_id"], json.dumps(data)))
            self._save(connection, data, "team.accepted", initial=True)

    def runs(self) -> list[dict]:
        with self.store.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM teams")]
        data = [self.get(team_id) for team_id in ids]
        return [{key: item[key] for key in ("team_id", "workspace", "status", "updated_at")}
                for item in sorted(data, key=lambda value: value["updated_at"], reverse=True)]

    def _save(self, connection, data: dict, event: str, *, initial=False) -> None:
        row = connection.execute("SELECT data FROM teams WHERE id=?", (data["team_id"],)).fetchone()
        if not initial:
            self._read(connection, data["team_id"])
        if row is None or strict_json_loads(row[0])["revision"] != data["revision"]:
            raise ContractError("Stale team update; reload its checkpoint")
        previous = connection.execute("SELECT envelope FROM team_events WHERE id=? ORDER BY revision DESC LIMIT 1",
                                      (data["team_id"],)).fetchone()
        data.update(revision=data["revision"] + 1, updated_at=utc_now())
        payload = {"kind": "team-checkpoint", "event": event, "data": data,
                   "previous_digest": canonical_digest(strict_json_loads(previous[0])) if previous else None}
        envelope = self.store.authorities.sign("controller", "event", payload)
        connection.execute("INSERT INTO team_events VALUES(?,?,?)", (data["team_id"], data["revision"], json.dumps(envelope)))
        connection.execute("UPDATE teams SET data=? WHERE id=?", (json.dumps(data), data["team_id"]))

    def save(self, data: dict, event: str) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._save(connection, data, event)

    @contextmanager
    def writer(self, team_id: str, *, wait_seconds=0):
        deadline = time.monotonic() + wait_seconds
        while True:
            lock = self.store._lock(self.store.run_dir(team_id) / "team.lock", "Another operation owns this team")
            try:
                lock.__enter__()
                break
            except ContractError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        try:
            yield self.get(team_id)
        finally:
            lock.__exit__(None, None, None)

    def signal(self, team_id: str, value: str | None = None) -> str | None:
        self.get(team_id)
        with self.store.connect() as connection:
            if value is not None:
                if value not in {"", "PAUSED", "CANCELLED"}:
                    raise ContractError("Unknown team signal")
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute("SELECT signal FROM team_signals WHERE id=?", (team_id,)).fetchone()
                if previous and previous[0] == "CANCELLED":
                    return "CANCELLED"
                connection.execute("INSERT INTO team_signals VALUES(?,?) ON CONFLICT(id) DO UPDATE SET signal=excluded.signal",
                                   (team_id, value))
            row = connection.execute("SELECT signal FROM team_signals WHERE id=?", (team_id,)).fetchone()
        return row[0] if row and row[0] else None

    def audit(self, team_id: str) -> dict:
        previous, data = None, None
        with self.store.connect() as connection:
            rows = connection.execute("SELECT revision,envelope FROM team_events WHERE id=? ORDER BY revision", (team_id,)).fetchall()
        for index, (revision, raw) in enumerate(rows, 1):
            payload = self._verify(raw, team_id)
            data = payload["data"]
            if revision != index or data["revision"] != index or payload["previous_digest"] != previous:
                raise ContractError("Team event chain is incomplete or inconsistent")
            previous = canonical_digest(strict_json_loads(raw))
        if data is None or data != self.get(team_id):
            raise ContractError("Team audit differs from its projection")
        return data
