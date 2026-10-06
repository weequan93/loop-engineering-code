"""Authenticated host-supervisor checkpoints; separate from task evidence."""

import json
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError
from .store import utc_now


class SupervisorStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS host_supervisors(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS host_supervisor_events(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY(id, revision));
            """)

    def _verify(self, raw, identity):
        payload = self.store.authorities.verify(strict_json_loads(raw), "event", key_id="controller")
        if payload.get("kind") != "native-host-supervisor" or payload["data"]["id"] != identity:
            raise ContractError("Invalid host supervisor checkpoint identity")
        return payload

    def _get(self, db, identity):
        row = db.execute("""SELECT data, (SELECT envelope FROM host_supervisor_events
            WHERE id=host_supervisors.id ORDER BY revision DESC LIMIT 1)
            FROM host_supervisors WHERE id=?""", (identity,)).fetchone()
        if not row or not row[1]:
            raise ContractError("Host supervisor does not exist")
        data = strict_json_loads(row[0])
        if data != self._verify(row[1], identity)["data"]:
            raise ContractError("Host supervisor differs from authenticated checkpoint")
        return data

    def get(self, identity):
        with self.store.connect() as db:
            return self._get(db, identity)

    def rows(self, project):
        with self.store.connect() as db:
            data = [self._get(db, row[0]) for row in db.execute("SELECT id FROM host_supervisors").fetchall()]
        return [row for row in data if row["project"] == str(project)]

    def _save(self, db, data, event, previous=None):
        data.update(revision=data["revision"] + 1, updated_at=utc_now())
        envelope = self.store.authorities.sign("controller", "event", {
            "kind": "native-host-supervisor", "event": event, "data": data,
            "previous_digest": canonical_digest(strict_json_loads(previous)) if previous else None})
        db.execute("INSERT INTO host_supervisor_events VALUES(?,?,?)",
                   (data["id"], data["revision"], json.dumps(envelope)))
        db.execute("INSERT INTO host_supervisors VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (data["id"], json.dumps(data)))

    def create(self, data):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for (identity,) in db.execute("SELECT id FROM host_supervisors").fetchall():
                row = self._get(db, identity)
                if row["project"] == data["project"] and row["status"] not in {"COMPLETE", "CANCELLED"}:
                    raise ContractError("An existing supervisor owns this project; resume its original ledger: " + identity)
            data.update(id="supervisor-" + uuid4().hex, revision=0)
            self._save(db, data, "supervisor.authorized")
        return data

    def update(self, identity, event, change):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            data = self._get(db, identity)
            previous = db.execute("SELECT envelope FROM host_supervisor_events WHERE id=? ORDER BY revision DESC LIMIT 1",
                                  (identity,)).fetchone()[0]
            change(data)
            self._save(db, data, event, previous)
        return data

    def audit(self, identity):
        with self.store.connect() as db:
            rows = db.execute("SELECT revision,envelope FROM host_supervisor_events WHERE id=? ORDER BY revision",
                              (identity,)).fetchall()
        previous, data = None, None
        for index, (revision, raw) in enumerate(rows, 1):
            payload = self._verify(raw, identity)
            data = payload["data"]
            if revision != index or data["revision"] != index or payload["previous_digest"] != previous:
                raise ContractError("Host supervisor history is inconsistent")
            previous = canonical_digest(strict_json_loads(raw))
        if data is None or data != self.get(identity):
            raise ContractError("Host supervisor history differs from projection")
        return data


def existing_summary(store, project):
    """Read existing supervisors without installing tables or launching processes."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_supervisors'").fetchone():
            return None
        rows = db.execute("""SELECT data, (SELECT envelope FROM host_supervisor_events
            WHERE id=host_supervisors.id ORDER BY revision DESC LIMIT 1) FROM host_supervisors""").fetchall()
    results = []
    for raw, signed in rows:
        data = strict_json_loads(raw)
        payload = store.authorities.verify(strict_json_loads(signed), "event", key_id="controller")
        if payload.get("kind") != "native-host-supervisor" or payload.get("data") != data:
            raise ContractError("Host supervisor projection is unauthenticated")
        if data["project"] == str(project):
            results.append(data)
    if not results:
        return None
    latest = max(results, key=lambda row: row["updated_at"])
    return {key: latest[key] for key in ("id", "status", "reason", "turns", "deadline_epoch", "team_id", "last_report")}

