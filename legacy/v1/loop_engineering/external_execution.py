"""Registered supervised executors, immutable artifacts and crash-safe admission."""

import json
import math
from pathlib import Path
import sys
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, ROOT, relative_path
from .evaluators import sign_result
from .execution_tools import configured_tools, output_identity
from .provision import copy_dependencies, ensure_dependencies
from .workspace import atomic_write, byte_digest, safe_path

MAX_ARTIFACT = 10485760


def executor_for(data, check_id):
    config = configured_tools(Path(data["workspace"]), data["profile"])
    check = next((item for item in data["task"]["checks"] if item["id"] == check_id), None)
    return next((item for item in (config or {}).get("executors", []) if item["check_id"] == check_id
                 and check and {"browser": "interaction", "http_load": "artifact"}.get(item["kind"]) == check["type"]), None)


def inspect_report(report, plan, request):
    """Recompute acceptance from raw samples/assertions, never a declared pass."""
    if report.get("bindings") != request or report.get("plan_digest") != canonical_digest(plan):
        raise ContractError("Executor report belongs to a different request or plan")
    if report.get("result") not in {"pass", "fail", "inconclusive"}:
        raise ContractError("Invalid executor verdict")
    if plan["kind"] == "http_load":
        samples = report.get("samples")
        if type(samples) is not list or len(samples) > plan["requests"]:
            raise ContractError("Invalid workload samples")
        for index, row in enumerate(samples):
            if (set(row) != {"index", "status", "bytes", "latency_us", "error"}
                    or row["index"] != index or type(row["latency_us"]) is not int or row["latency_us"] < 1
                    or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= plan["max_response_bytes"] + 1
                    or (row["status"] is not None and (type(row["status"]) is not int or not 100 <= row["status"] <= 599))
                    or (row["error"] is not None and type(row["error"]) is not str)):
                raise ContractError("Malformed workload sample")
        latencies = sorted(row["latency_us"] for row in samples)
        percentile = lambda p: latencies[max(0, math.ceil(len(latencies) * p / 100) - 1)] if latencies else None
        errors = sum(row["error"] is not None or row["status"] != plan["expected_status"]
                     or row["bytes"] > plan["max_response_bytes"] for row in samples)
        metrics = report.get("metrics", {})
        expected = {"requests": len(samples), "successes": len(samples) - errors, "errors": errors,
                    "p50_us": percentile(50), "p95_us": percentile(95), "p99_us": percentile(99)}
        if any(metrics.get(key) != value for key, value in expected.items()):
            raise ContractError("Workload metrics do not match its raw samples")
        if report.get("thresholds") != plan["thresholds"] or report.get("workload") != {key: plan[key] for key in
                ("requests", "concurrency", "requests_per_second", "expected_status", "max_response_bytes")}:
            raise ContractError("Workload or thresholds changed during execution")
        passed = (len(samples) == plan["requests"] and expected["successes"] >= plan["thresholds"]["min_successes"]
                  and errors <= plan["thresholds"]["max_errors"] and expected["p95_us"] is not None
                  and expected["p95_us"] <= plan["thresholds"]["max_p95_ms"] * 1000)
        if report["result"] != ("pass" if passed else "fail"):
            raise ContractError("Workload verdict contradicts the predeclared thresholds")
    else:
        rows = report.get("steps")
        if type(rows) is not list or len(rows) > len(plan["steps"]):
            raise ContractError("Invalid browser steps")
        for row, step in zip(rows, plan["steps"]):
            if (any(row.get(key) != value for key, value in step.items())
                    or row.get("status") not in {"pass", "fail"}):
                raise ContractError("Browser procedure changed during execution")
            if row["status"] == "pass" and step["action"] in {"assert_text", "assert_count"}:
                expected = int(step["value"]) if step["action"] == "assert_count" else step["value"]
                if row.get("observed") != expected:
                    raise ContractError("Browser assertion contradicts its observed value")
        if report["result"] == "pass" and (len(rows) != len(plan["steps"]) or report.get("blocked_requests")
                or any(row["status"] != "pass" for row in rows)
                or not {"screenshot.png", "trace.zip"}.issubset(report.get("artifacts", []))):
            raise ContractError("Browser pass lacks complete assertions or retained captures")
    return report["result"]


def execute_evaluation(controller, run_id, check_id):
    data = controller.store.get(run_id)
    controller._assert_contract(data)
    check = next((check for check in data["task"]["checks"] if check["id"] == check_id), None)
    plan = executor_for(data, check_id)
    if not plan or not check or check["type"] != {"browser": "interaction", "http_load": "artifact"}[plan["kind"]]:
        raise ContractError("No matching registered executor for this check/type")
    if "run_checks" not in data["task"]["authorization"]["allowed_actions"]:
        raise ContractError("Task does not authorize external check execution")
    if data["task"]["autonomy"] == "unattended" or data["native"]["config"]["runtime"]["backend"] != "local":
        raise ContractError("These executors require the supervised local runtime")
    key = next((item for item in data["native"]["config"]["evaluator_keys"]
                if item["role"] == check["type"] and check_id in item["check_ids"]), None)
    if key is None:
        raise ContractError("Executor has no registered evaluator authority")
    root = Path(data["workspace"])
    snapshot = controller.snapshots.capture(root, data["profile"])
    environment = controller.environment_digest(data)
    current = data["native"]["external_by_check"].get(check_id)
    if current:
        record = controller.store.evidence(run_id, [current])[0]
        if record["snapshot_digest"] == snapshot["digest"] and record["environment_digest"] == environment:
            controller.store.authorities.verify(data["native"]["external_origins"][current], "evidence")
            controller._check_artifacts([record])
            return record
    identity = check_id + ":" + snapshot["digest"] + ":" + environment
    saved = data["native"].get("execution_attempts", {}).get(identity)
    if saved and saved["status"] == "RUNNING":
        raise ContractError("Executor effect is unresolved; inspect and reconcile action " + saved.get("action_id", saved["id"]))
    if saved and saved["status"] == "RESULT":
        controller.import_evaluation(run_id, saved["envelope"])
        return controller.store.evidence(run_id, [controller.store.get(run_id)["native"]["external_by_check"][check_id]])[0]
    request = controller.evaluation_request(run_id, check_id)
    with controller.store.writer(run_id) as data:
        if not controller._preflight(data):
            raise ContractError("Executor stopped by signal or task budget")
        if data["pending_process"] or data["pending_edit"]:
            raise ContractError("Reconcile pending effects before running an evaluator")
        with controller.timed_operation(data, "external_executor"):
            ensure_dependencies(controller, data)
            if (controller.snapshots.capture(root, data["profile"])["digest"] != snapshot["digest"]
                    or controller.environment_digest(data) != environment):
                raise ContractError("Executor candidate/environment changed before dispatch")
            attempt = {"id": "executor-" + uuid4().hex, "status": "RUNNING", "request": request,
                       "plan_digest": canonical_digest(plan)}
            if saved:
                attempt["history"] = [*saved.get("history", []), {k: v for k, v in saved.items() if k != "history"}]
            if len(attempt.get("history", [])) >= 8:
                raise ContractError("External executor attempt limit reached")
            data["native"].setdefault("execution_attempts", {})[identity] = attempt
            data["active_executor"] = identity
            directory = controller.store.run_dir(run_id) / attempt["id"]
            work = directory / "workspace"
            controller.snapshots.materialize(snapshot, work)
            config = configured_tools(root, data["profile"])
            dependencies = copy_dependencies(root, work, config, data["profile"])
            artifacts = directory / "artifacts"
            artifacts.mkdir(parents=True)
            job = {"plan": plan, "plan_digest": canonical_digest(plan), "request": request["payload"],
                   "workspace": str(work), "artifacts": str(artifacts),
                   "remaining_ms": max(1, int(controller.remaining_seconds(data) * 1000))}
            if plan["kind"] == "browser":
                if plan["document"]:
                    document = safe_path(work, plan["document"])
                    if not document.is_file() or document.stat().st_size > 1048576:
                        raise ContractError("Browser document is absent or exceeds 1 MiB")
                    job["html"] = document.read_text(encoding="utf-8")
                argv = [plan["node"], str(ROOT / "scripts/browser_check.cjs")]
            else:
                argv = [sys.executable, "-B", str(ROOT / "scripts/execute_check.py")]
            controller.store.save(data, "executor.dispatch_requested", {"identity": identity, "kind": plan["kind"]})
            result = controller.launch(data, argv, work, directory / "logs",
                timeout=min(plan["timeout_seconds"], controller.remaining_seconds(data)), kind="evaluator",
                input_text=json.dumps(job), max_output_bytes=2097152)
            attempt.update(action_id=data["last_process_action_id"], outcome=result.outcome, exit_code=result.exit_code,
                           elapsed_ms=result.elapsed_ms)
            paths = [result.stdout, result.stderr]
            verdict = "inconclusive"
            detail = plan["kind"] + ": " + result.outcome + ", exit=" + str(result.exit_code)
            if result.outcome == "completed" and result.exit_code == 0:
                response = strict_json_loads(result.stdout.read_text())
                if set(response) != {"result", "artifacts"} or type(response["artifacts"]) is not list or not 1 <= len(response["artifacts"]) <= 3:
                    raise ContractError("Invalid executor response")
                if "report.json" not in response["artifacts"] or len(set(response["artifacts"])) != len(response["artifacts"]):
                    raise ContractError("Executor report is absent or duplicate")
                for name in response["artifacts"]:
                    relative_path(name)
                    if name not in {"report.json", "screenshot.png", "trace.zip"}:
                        raise ContractError("Unsupported executor artifact")
                    artifact = safe_path(artifacts, name)
                    if not artifact.is_file() or artifact.stat().st_size > MAX_ARTIFACT:
                        raise ContractError("Executor artifact missing or exceeds 10 MiB")
                    paths.append(artifact)
                report = strict_json_loads((artifacts / "report.json").read_text())
                verdict = inspect_report(report, plan, request["payload"])
                if response["result"] != verdict:
                    raise ContractError("Executor response/report verdict mismatch")
                detail += ", " + verdict
            if (controller.snapshots.capture(work, data["profile"])["digest"] != snapshot["digest"]
                    or (dependencies is not None and output_identity(work, config["provision"], data["profile"]) != dependencies)
                    or controller.snapshots.capture(root, data["profile"])["digest"] != snapshot["digest"]
                    or controller.environment_digest(data) != environment):
                raise ContractError("Executor mutated its source/dependencies or current environment")
            if controller.stop_signal(data) or controller.remaining_seconds(data) <= 0:
                verdict = "inconclusive"
            # Journal the actual process outcome even when it produced no report.
            outcome_path = directory / "outcome.json"
            atomic_write(outcome_path, json.dumps({"request": request["payload"], "plan_digest": canonical_digest(plan),
                "outcome": result.outcome, "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
                "result": verdict}).encode())
            paths.append(outcome_path)
            envelope = sign_result(controller.store.authorities, request, key["key_id"], result=verdict,
                                   summary=detail, artifacts=paths, findings=[])
            attempt.update(status="RESULT", envelope=envelope)
            data["active_executor"] = None
            controller.store.save(data, "executor.result_cached", {"identity": identity, "result": verdict})
    controller.import_evaluation(run_id, envelope)
    return controller.store.evidence(run_id, [controller.store.get(run_id)["native"]["external_by_check"][check_id]])[0]
