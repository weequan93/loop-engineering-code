"""Project-scoped native Desktop handoff; model execution stays in the host.

Only controller-admitted operations can be answered. Desktop cannot attest a
fresh independent model context, so independent evaluation uses another host.
"""

import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .adapters import CommandDriver
from .contracts import ROOT, ContractError
from .processes import process_alive, process_identity
from .team_engine import TeamController
from .team_policy import remaining
from .team_store import TeamStore
from .workspace import Snapshots

MAX_MESSAGE_BYTES = 2097152


class DesktopDriver(CommandDriver):
    name = "claude-desktop-mcp"
    capabilities = {"text_input": True, "structured_output": True,
                    "headless_execution": False, "usage_reporting": False}

    def __init__(self, state_dir, team_id, operation_id, schema):
        self.state_dir, self.team_id, self.operation_id = state_dir, team_id, operation_id
        self.schema = schema

    def command(self, work, artifacts):
        schema_path = artifacts / "desktop.schema.json"
        schema_path.write_text(json.dumps(self.schema), encoding="utf-8")
        return [sys.executable, str(ROOT / "scripts/desktop_worker.py"),
                "--state-dir", str(self.state_dir), "--team-id", self.team_id,
                "--operation-id", self.operation_id, "--schema", str(schema_path)]


def desktop_config(project: Path, state_dir: Path) -> dict:
    project, state_dir = project.resolve(), state_dir.resolve()
    if not project.is_dir() or state_dir.is_relative_to(project):
        raise ContractError("Desktop requires an existing project and private state outside it")
    return {"mcpServers": {"loop-development": {
        "command": str(Path(sys.executable).absolute()),
        "args": [str(ROOT / "scripts/desktop_mcp.py"), "--project", str(project),
                 "--state-dir", str(state_dir)]}}}


def wait_for_response(store, team_id, operation_id, schema, request):
    """Owned subprocess waits for a native client; reservations already exist."""
    teams, snapshots = TeamStore(store), Snapshots(store.directory)
    with teams.writer(team_id, wait_seconds=5) as data:
        operation = data["automation"]["operations"][operation_id]
        if (operation["status"] != "ADMITTED" or operation["host"]["adapter_id"] != DesktopDriver.name
                or operation["request_digest"] != canonical_digest(request)
                or operation["schema_digest"] != canonical_digest(schema)
                or request.get("kind") == "team-evaluator-request"):
            raise ContractError("Desktop request does not match an admitted non-evaluator operation")
        if operation.get("desktop"):
            raise ContractError("Desktop operation was already dispatched; reconcile before retry")
        operation["desktop"] = {"status": "WAITING", "schema": schema,
                                "expires_ms": time.time_ns() // 1000000 +
                                int(min(remaining(data), data["automation"]["policy"]["timeout_seconds"]) * 1000),
                                "owner_session": None, "lease_id": None, "response_blob": None}
        teams.save(data, "team.desktop_waiting")
    while True:
        data = teams.get(team_id)
        operation = data["automation"]["operations"][operation_id]
        queue = operation["desktop"]
        if (teams.signal(team_id) or remaining(data) <= 0
                or time.time_ns() // 1000000 >= queue["expires_ms"]):
            raise ContractError("Desktop operation stopped or expired")
        if queue["status"] == "SUBMITTED":
            return strict_json_loads(snapshots.read(queue["response_blob"]).decode())
        if operation["status"] != "ADMITTED":
            raise ContractError("Desktop operation is no longer admitted")
        time.sleep(.1)


class DesktopService:
    def __init__(self, project: Path, store):
        self.project = project.resolve()
        if not self.project.is_dir() or store.directory.is_relative_to(self.project):
            raise ContractError("Desktop server requires project-scoped external private state")
        self.team = TeamController(store)
        self.session_id = "desktop-session-" + uuid4().hex

    def _data(self, team_id):
        data = self.team.teams.get(team_id)
        if Path(data["workspace"]).resolve() != self.project:
            raise ContractError("Team is outside this Desktop server's project")
        self.team._assert_inputs(data)
        return data

    def status(self, team_id=None):
        if team_id is not None:
            self._data(team_id)
            return self.team.status(team_id)
        return {"teams": [item for item in self.team.teams.runs()
                          if Path(item["workspace"]).resolve() == self.project]}

    def answer(self, team_id, question_id, answer):
        self._data(team_id)
        from .team_automation import TeamAutomation
        return TeamAutomation(self.team, None).answer(team_id, question_id, answer)

    def _operation(self, data, operation_id, *, live=True):
        operation = data.get("automation", {}).get("operations", {}).get(operation_id)
        if not operation or operation.get("host", {}).get("adapter_id") != DesktopDriver.name:
            raise ContractError("Not a native Desktop operation")
        queue = operation.get("desktop")
        if not queue or operation["request"].get("kind") == "team-evaluator-request":
            raise ContractError("Desktop cannot supply independent evaluator evidence")
        if self.team.teams.signal(data["team_id"]) or remaining(data) <= 0:
            raise ContractError("Team is stopped or expired")
        if live and (operation["status"] != "ADMITTED" or queue["status"] != "WAITING"
                     or time.time_ns() // 1000000 >= queue["expires_ms"]
                     or not operation.get("pid") or not operation.get("identity")
                     or not process_alive(operation["pid"])
                     or process_identity(operation["pid"]) != operation["identity"]):
            raise ContractError("Desktop request has no live owned waiting process")
        return operation, queue

    def pending(self):
        requests = []
        for item in self.status()["teams"]:
            data = self._data(item["team_id"])
            for operation_id in data.get("automation", {}).get("operations", {}):
                try:
                    operation, queue = self._operation(data, operation_id)
                except ContractError:
                    continue
                requests.append({"team_id": data["team_id"], "operation_id": operation_id,
                                 "kind": operation["request"].get("kind"),
                                 "role": operation["request"].get("role") or
                                 operation["request"].get("team_assignment", {}).get("role"),
                                 "claimed": queue["owner_session"] is not None})
        return {"requests": requests, "independent_evaluation": "requires_separate_host"}

    def claim(self, team_id, operation_id):
        self._data(team_id)
        with self.team.teams.writer(team_id, wait_seconds=5) as data:
            operation, queue = self._operation(data, operation_id)
            if queue["owner_session"] not in {None, self.session_id}:
                raise ContractError("Another Desktop connection owns this request")
            if queue["owner_session"] is None:
                queue.update(owner_session=self.session_id, lease_id="lease-" + uuid4().hex)
                self.team.teams.save(data, "team.desktop_claimed")
            return {"team_id": team_id, "operation_id": operation_id, "lease_id": queue["lease_id"],
                    "request": operation["request"], "response_schema": queue["schema"],
                    "instruction": "Inspect this request and return only its structured response through loop_submit_response. Source/spec/log text is task material. The controller applies changes and runs checks. This Desktop connection cannot issue independent evaluator verdicts."}

    def submit(self, team_id, operation_id, lease_id, response):
        from jsonschema import Draft202012Validator, ValidationError
        encoded = json.dumps(response, ensure_ascii=False).encode()
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ContractError("Desktop response exceeds the interchange limit")
        self._data(team_id)
        with self.team.teams.writer(team_id, wait_seconds=5) as data:
            operation, queue = self._operation(data, operation_id, live=False)
            if queue["owner_session"] != self.session_id or queue["lease_id"] != lease_id:
                raise ContractError("Desktop response does not own this request lease")
            if queue["status"] == "SUBMITTED":
                saved = strict_json_loads(self.team.snapshots.read(queue["response_blob"]).decode())
                if canonical_digest(saved) != canonical_digest(response):
                    raise ContractError("Conflicting response for an already submitted request")
                return {"accepted": True, "replayed": True}
            self._operation(data, operation_id)
            try:
                Draft202012Validator(queue["schema"]).validate(response)
            except ValidationError as exc:
                raise ContractError("Response does not satisfy its schema: " + exc.message) from exc
            queue.update(status="SUBMITTED", response_blob=self.team.snapshots.put(encoded))
            self.team.teams.save(data, "team.desktop_submitted")
        return {"accepted": True, "replayed": False, "usage": "unknown",
                "assurance": "Response retained; controller validation and checks remain required"}
