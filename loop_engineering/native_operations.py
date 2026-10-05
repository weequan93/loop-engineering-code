"""Durable admission for long native checks; no model scheduler or result synthesis.

One OS lock covers admission, execution and final journaling. A disconnected client
can observe the same operation. A dead server leaves an interrupted journal; recovery
reconciles the existing child and never replays a possibly applied proposal.
"""

import json
import threading
import time
from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError


class NativeOperations:
    def __init__(self, service):
        self.service = service
        self.local = threading.local()
        self.threads = {}
        self.mutex = threading.Lock()

    def lock(self, team_id):
        # Validate membership before using an identifier in a filesystem path.
        self.service._data(team_id, native=True)
        key = canonical_digest(team_id).split(":")[-1]
        return self.service.team.store._lock(
            self.service.team.store.directory / "native-operations" / (key + ".lock"),
            "A native operation still owns this team; wait for its recorded result")

    def alive(self, team_id):
        try:
            with self.lock(team_id):
                return False
        except ContractError as exc:
            if "still owns this team" not in str(exc):
                raise
            return True

    def assert_idle(self, data):
        if getattr(self.local, "team_id", None) == data["team_id"]:
            return
        if any(row["status"] == "RUNNING" for row in data["native_host"].get("operations", {}).values()):
            raise ContractError("Native operation pending; inspect loop_operation_status and wait or recover it")

    def snapshot(self, data):
        rows = list(data.get("native_host", {}).get("operations", {}).values())
        running = any(row["status"] == "RUNNING" for row in rows)
        alive = self.alive(data["team_id"]) if running else False
        result = []
        for row in rows[-20:]:
            view = {k: v for k, v in row.items() if k not in {"payload", "key"}}
            view["elapsed_seconds"] = max(0, (row.get("finished_epoch") or self.service.clock()) - row["started_epoch"])
            view["recovery_required"] = row["status"] == "RUNNING" and not alive
            view["runner_alive"] = row["status"] == "RUNNING" and alive
            result.append(view)
        return result

    def status(self, team_id, operation_id=None, wait_seconds=0):
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 5:
            raise ContractError("Operation waits must be between 0 and 5 seconds")
        end = time.monotonic() + wait_seconds
        while True:
            data = self.service._data(team_id, native=True)
            rows = self.snapshot(data)
            if operation_id:
                rows = [row for row in rows if row["id"] == operation_id]
                if not rows:
                    # Old records remain available even after the compact list rolls over.
                    original = data["native_host"].get("operations", {}).get(operation_id)
                    if original is None:
                        raise ContractError("Unknown native operation")
                    rows = self.snapshot({**data, "native_host": {"operations": {operation_id: original}}})
            if not any(row["runner_alive"] for row in rows) or time.monotonic() >= end:
                return {"team_id": team_id, "operations": rows,
                        "assurance": "Operation elapsed time only; completion requires the original checks and handoffs"}
            time.sleep(min(0.1, max(0, end - time.monotonic())))

    def start(self, kind, arguments):
        if kind not in {"task_submit", "workbench_submit", "evaluation"}:
            raise ContractError("Unsupported native operation")
        # Copy before returning so a caller cannot mutate the admitted operation.
        arguments = json.loads(json.dumps(arguments))
        if len(json.dumps(arguments).encode()) > 1100000:
            raise ContractError("Native operation payload exceeds its byte limit")
        team_id = arguments["team_id"]
        data = self.service._data(team_id, native=True)
        key = canonical_digest({"kind": kind, "arguments": arguments})
        for row in reversed(list(data["native_host"].get("operations", {}).values())):
            if row["key"] == key and (kind != "evaluation" or row["status"] == "RUNNING"):
                return {**self.status(team_id, row["id"]), "operation_id": row["id"]}
        lock = self.lock(team_id)
        lock.__enter__()
        try:
            self.service._active(team_id)
            payload = arguments
            if kind == "workbench_submit":
                from .native_workbench import prepare_submission
                payload = prepare_submission(self.service, **arguments)
            operation_id = "operation-" + uuid4().hex
            with self.service.team.teams.writer(team_id) as current:
                self.service.team._assert_inputs(current)
                if self.service.team.teams.signal(team_id) or current["status"] != "ACTIVE":
                    raise ContractError("Stopped team cannot admit a background operation")
                rows = current["native_host"].setdefault("operations", {})
                if any(r["status"] == "RUNNING" for r in rows.values()):
                    raise ContractError("Recover the original operation before admitting another")
                if len(rows) >= 128:
                    raise ContractError("Native operation history limit reached; preserve history and review the batch")
                rows[operation_id] = {"id": operation_id, "key": key, "kind": kind,
                    "task_id": arguments["task_id"], "status": "RUNNING",
                    "started_epoch": int(self.service.clock()), "finished_epoch": None, "error": None,
                    "payload": self.service.team.snapshots.put(json.dumps(payload).encode())}
                self.service.team.teams.save(current, "host.operation_admitted")
            thread = threading.Thread(target=self._run, args=(team_id, operation_id, lock),
                                      name="loop-" + operation_id, daemon=True)
            with self.mutex:
                self.threads[operation_id] = (team_id, thread)
            thread.start()
        except BaseException:
            lock.__exit__(None, None, None)
            raise
        return {"team_id": team_id, "operation_id": operation_id, "status": "RUNNING",
                "next_action": {"kind": "wait_operation", "tool": "loop_operation_status",
                    "arguments": {"team_id": team_id, "operation_id": operation_id, "wait_seconds": 5},
                    "continue_work": True}}

    def _run(self, team_id, operation_id, lock):
        self.local.team_id = team_id
        error = None
        returned = False
        try:
            row = self.service._data(team_id, native=True)["native_host"]["operations"][operation_id]
            payload = json.loads(self.service.team.snapshots.read(row["payload"]))
            if row["kind"] == "workbench_submit":
                from .native_workbench import finish_submission
                self.service.task_submit(**{k: payload[k] for k in ("team_id", "task_id", "request_id", "step")})
                finish_submission(self.service, payload)
            elif row["kind"] == "task_submit":
                self.service.task_submit(**payload)
            else:
                self.service.execute_evaluation(**payload)
            returned = True
        except Exception as exc:
            error = str(exc)[:2048]
            returned = True
        finally:
            try:
                if returned:
                    with self.service.team.teams.writer(team_id, wait_seconds=5) as current:
                        current["native_host"]["operations"][operation_id].update(
                            status="FAILED" if error else "FINISHED", error=error, finished_epoch=int(self.service.clock()))
                        self.service.team.teams.save(current, "host.operation_finished")
            finally:
                self.local.team_id = None
                lock.__exit__(None, None, None)
                with self.mutex:
                    self.threads.pop(operation_id, None)

    def recover(self, team_id, operation_id):
        with self.lock(team_id):
            data = self.service._data(team_id, native=True)
            row = data["native_host"].get("operations", {}).get(operation_id)
            if row is None:
                raise ContractError("Unknown native operation")
            if row["status"] != "RUNNING":
                return self.status(team_id, operation_id)
            if self.service.team.teams.signal(team_id) or data["status"] != "ACTIVE":
                raise ContractError("Stopped team requires the user's resume instruction before recovery")
            # Existing recovery reconciles owned processes/edits and charges elapsed time.
            # It refuses ambiguous effects and never grants a fresh child budget.
            self.service.team.resume(team_id)
            child_id = data["records"][row["task_id"]]["child_run_id"]
            if data["records"][row["task_id"]]["status"] == "RUNNING" and child_id and self.service.team._exists(child_id):
                if self.service.team.store.get(child_id)["state"]["status"] == "SUCCEEDED":
                    self.service.team.collect(team_id, row["task_id"])
            with self.service.team.teams.writer(team_id) as current:
                current["native_host"]["operations"][operation_id].update(status="RECOVERED",
                    finished_epoch=int(self.service.clock()), error="Interrupted operation reconciled; proposal not replayed")
                self.service.team.teams.save(current, "host.operation_recovered")
        return self.service.progress(team_id)

    def close(self):
        # A stopped MCP process is not an unattended scheduler. Preserve recoverable
        # state and ask existing controllers to clean up their own processes.
        with self.mutex:
            active = list(self.threads.values())
        for team_id in {team_id for team_id, _ in active}:
            if self.service._data(team_id)["status"] not in {"COMPLETE", "CANCELLED"}:
                self.service.team.stop(team_id)
        for _, thread in active:
            thread.join(timeout=2)
