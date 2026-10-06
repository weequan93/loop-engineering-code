"""A provider-neutral command bridge and a concrete Codex CLI proposal driver."""

import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from reference.core import canonical_digest, stop_decision, strict_json_loads
from .contracts import ROOT, ContractError, step_schema, validate_step
from .output_schema import structured_output_schema
from .recovery import recovery_policy


class CommandDriver:
    name = "command"
    capabilities = {"text_input": True, "headless_execution": True}

    def __init__(self, argv: list[str]):
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item or "\0" in item for item in argv):
            raise ContractError("Agent command must be a nonempty argv array")
        self.argv = argv

    def command(self, work: Path, artifacts: Path) -> list[str]:
        return self.argv

    def prepare_context(self, context: dict) -> dict:
        return context

    def describe(self) -> dict:
        return {"schema_version": "0.1", "adapter_id": self.name, "kind": "agent", "backend": self.name,
                "version": "0.2.0", "control_model": "externally_managed", "capabilities": dict(self.capabilities),
                "extensions": {"protocol": "agent-step-v0.2", "live_backend_conformance": "unverified"}}

    def response(self, result, artifacts: Path) -> dict:
        if result.stdout.stat().st_size > 2097152:
            raise ContractError("Agent response exceeds the 2 MiB interchange limit")
        return strict_json_loads(result.stdout.read_text(encoding="utf-8"))

    def tokens(self, result) -> int | None:
        return None

    def token_report(self, result) -> dict:
        value = self.tokens(result)
        return {"tokens": value, "known_tokens": value if value is not None else 0}


class CodexDriver(CommandDriver):
    name = "codex-cli"
    capabilities = {"text_input": True, "headless_execution": True, "structured_output": True,
                    "structured_events": True, "usage_reporting": True}

    def __init__(self, *, executable: str = "codex", model: str | None = None):
        if not shutil.which(executable):
            raise ContractError("Codex CLI is unavailable; use the file bridge or a configured command adapter")
        self.executable, self.model = executable, model

    def command(self, work: Path, artifacts: Path) -> list[str]:
        # The backend only proposes changes; our broker applies them after
        # validating the contract, candidate identity, scope, and file hashes.
        schema = artifacts / "step.schema.json"
        schema.write_text(json.dumps(structured_output_schema(
            getattr(self, "proposal_schema", strict_json_loads((ROOT / "schemas/step-v0.2.schema.json").read_text())))), encoding="utf-8")
        argv = [self.executable, "--no-daemon", "--ask-for-approval", "never", "exec",
                "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--json", "--output-schema", str(schema),
                "--output-last-message", str(artifacts / "response.json"), "--cd", str(work)]
        if self.model:
            argv.extend(["--model", self.model])
        return argv + ["-"]

    def prepare_context(self, context: dict) -> dict:
        self.proposal_schema = step_schema(context.get("bundle", context)["task"])
        return context

    def response(self, result, artifacts: Path) -> dict:
        path = artifacts / "response.json"
        if not path.is_file() or path.stat().st_size > 2097152:
            raise ContractError("Codex did not produce a bounded structured response")
        return strict_json_loads(path.read_text(encoding="utf-8"))

    def tokens(self, result) -> int | None:
        return self.token_report(result)["tokens"]

    def token_report(self, result) -> dict:
        total, found, complete = 0, False, True
        for line in result.stdout.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                found = True
                usage = event.get("usage", {})
                if not isinstance(usage, dict):
                    complete = False
                    continue
                counts = (usage.get("input_tokens"), usage.get("output_tokens"))
                if all(type(value) is int and value >= 0 for value in counts):
                    total += sum(counts)
                else:
                    complete = False
            elif event.get("type") == "turn.failed":
                complete = False
        return {"tokens": total if found and complete else None, "known_tokens": total}


def drive(controller, run_id: str, driver: CommandDriver, *, turns: int = 1, timeout: int = 180) -> dict:
    if turns < 1 or timeout < 1:
        raise ContractError("Turns and agent timeout must be positive")
    for _ in range(turns):
        with controller.store.writer(run_id) as data:
            if data["state"]["status"] != "PLANNING":
                return data
            missing = {name for name in data["task"]["required_capabilities"]["agent"] if driver.capabilities.get(name) is not True}
            if missing:
                raise ContractError("Required agent capabilities unavailable: " + ", ".join(sorted(missing)))
            if not controller._preflight(data, implementation=True):
                return data
            with controller.timed_operation(data, "agent"):
                context = driver.prepare_context(controller.context(run_id))
                snapshot = controller.snapshots.capture(Path(data["workspace"]), data["profile"])
                artifacts = controller.store.run_dir(run_id) / ("agent-" + uuid4().hex)
                artifacts.mkdir()
                work = artifacts / "workspace"
                controller.snapshots.materialize(snapshot, work)
                spent = controller.remaining_seconds(data)
                available = spent - controller.verification_reserve(data)
                if available <= 0:
                    controller.checkpoint(data, "BUDGET_EXHAUSTED", "Context preparation consumed the implementation budget")
                    return data
                argv = driver.command(work, artifacts)
                data["state"]["iteration"] += 1
                previous_usage_complete = data["usage_complete"]
                # A crash between dispatch/result/usage parsing must leave
                # incomplete coverage persisted, even if the PID was cleared.
                data["usage_complete"] = False
                data["state"]["usage"]["tokens"] = None
                controller.transition(data, "EXECUTING")
                controller.store.save(data, "agent.dispatched", {"adapter": driver.name, "iteration": data["state"]["iteration"]})
                serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
                prompt = "Use only the supplied context. Do not execute tools, edit files, or spawn agents. Return exactly one AgentStep JSON object.\n" + serialized
                result = controller.launch(data, argv, work, artifacts / "logs", timeout=min(timeout, available),
                                           kind="agent", input_text=prompt if driver.name == "codex-cli" else serialized)
                usage = driver.token_report(result)
                data["known_tokens"] += usage["known_tokens"]
                data["usage_complete"] = previous_usage_complete and usage["tokens"] is not None
                data["state"]["usage"]["tokens"] = data["known_tokens"] if data["usage_complete"] else None
                data["charged_step_digest"] = None
                if result.outcome != "completed" or result.exit_code != 0:
                    text = result.stderr.read_text(errors="replace").lower()
                    category = ("authentication" if any(word in text for word in ("unauthorized", "login", "authentication"))
                                else "permission" if any(word in text for word in ("permission denied", "operation not permitted"))
                                else "dependency" if result.outcome == "unavailable" else "transient")
                    data["last_recovery"] = recovery_policy(category)
                    previous = data.get("adapter_failure", {})
                    count = previous.get("count", 0) + 1 if previous.get("category") == category else 1
                    data["adapter_failure"] = {"category": category, "count": count}
                    reason = f"Agent dispatch {result.outcome}, exit={result.exit_code}; diagnostics: {result.stderr}"
                    stop = ("CANCELLED" if result.outcome == "cancelled" else "AWAITING_INPUT" if category in {"authentication", "permission"}
                            else "PLANNING" if category == "transient" and count == 1 else "BLOCKED")
                    step = None
                elif usage["tokens"] is None and "usage_reporting" in data["task"]["required_capabilities"]["agent"]:
                    step = None
                    data["last_recovery"] = recovery_policy("capability")
                    stop, reason = "BLOCKED", "Required usage_reporting was unavailable for this dispatch"
                else:
                    try:
                        step = driver.response(result, artifacts)
                        validate_step(step, data["task"], snapshot["digest"])
                        if controller.snapshots.capture(Path(data["workspace"]), data["profile"])["digest"] != snapshot["digest"]:
                            raise ContractError("Candidate changed during agent dispatch; proposal is stale")
                        controller.prepare_proposal(data, snapshot, step)
                        data["charged_step_digest"] = canonical_digest(step)
                        data["adapter_failure"] = {"category": None, "count": 0}
                        stop, reason = "PLANNING", "Bounded agent proposal received; controller will validate its effects"
                    except (OSError, ValueError) as exc:
                        step = None
                        data["last_recovery"] = recovery_policy("invalid_output")
                        previous = data.get("adapter_failure", {})
                        count = previous.get("count", 0) + 1 if previous.get("category") == "invalid_output" else 1
                        data["adapter_failure"] = {"category": "invalid_output", "count": count}
                        stop, reason = ("PLANNING" if count == 1 else "BLOCKED"), str(exc)
            if step is None and stop == "PLANNING":
                breaker = stop_decision(data["task"]["limits"], controller._usage(data),
                                        cancelled=controller.store.cancelled(run_id))
                if breaker:
                    stop, reason = breaker.outcome, "; ".join(breaker.reasons)
            controller.checkpoint(data, stop, reason)
        if step is None:
            if data["state"]["status"] == "PLANNING":
                continue
            return data
        data = controller.submit(run_id, step, agent_capabilities=driver.capabilities)
        if data["state"]["status"] != "PLANNING":
            return data
    return controller.store.get(run_id)
