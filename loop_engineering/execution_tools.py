"""Frozen execution configuration, bounded dependency identity and HTTP work."""

from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import re
import threading
import time
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from reference.core import canonical_digest
from .contracts import ContractError, load, relative_path, validate
from .workspace import byte_digest, matches, safe_path

TOOLS_PATH = ".loop/execution-tools.json"


def origin(url):
    if type(url) is not str or any(character.isspace() or ord(character) < 32 for character in url):
        raise ContractError("Executor URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ContractError("Invalid executor URL") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise ContractError("Executor target must be an HTTP(S) URL without credentials or fragments")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ContractError("Invalid target port") from exc
    if port is not None and port < 1:
        raise ContractError("Invalid target port")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    default = 80 if parsed.scheme == "http" else 443
    return parsed.scheme + "://" + host + (":" + str(port) if port and port != default else "")


def validate_tools(config):
    provision = config["provision"]
    ids = [step["id"] for step in provision["steps"]]
    if len(ids) != len(set(ids)) or provision["steps"] and (not provision["inputs"] or not provision["output_dirs"]):
        raise ContractError("Provisioning needs unique steps, pinned inputs and output directories")
    for name in [*provision["inputs"], *provision["output_dirs"]]:
        relative_path(name)
        if name.split("/")[0].casefold() in {".git", ".loop"}:
            raise ContractError("Execution tools cannot provision control or Git paths")
    for left in provision["output_dirs"]:
        if any(right != left and (left.startswith(right + "/") or right.startswith(left + "/"))
               for right in provision["output_dirs"]):
            raise ContractError("Provision output directories must not overlap")
        if any(name == left or name.startswith(left + "/") for name in provision["inputs"]):
            raise ContractError("Provision inputs cannot be mutable dependency outputs")
    for step in provision["steps"]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", step["id"]):
            raise ContractError("Provision step ID must be a bounded safe component")
        relative_path(step["cwd"], allow_dot=True)
        if any("\0" in argument for argument in step["argv"]):
            raise ContractError("Provision argv contains a NUL byte")
    checks = [item["check_id"] for item in config["executors"]]
    if len(checks) != len(set(checks)):
        raise ContractError("Each external check has one registered executor")
    for item in config["executors"]:
        for allowed in item["allowed_origins"]:
            if origin(allowed) != allowed or urlsplit(allowed).path or urlsplit(allowed).query:
                raise ContractError("Allowed targets must be canonical origins")
        if item["kind"] == "browser":
            if not any(step["action"].startswith("assert_") for step in item["steps"]):
                raise ContractError("Browser checks need at least one actual assertion")
            if item["browser_executable"] is None:
                raise ContractError("Browser checks need an explicit executable for runtime identity")
            if bool(item["document"]) == bool(item["url"]):
                raise ContractError("Browser check needs exactly one document or URL")
            if item["document"]:
                relative_path(item["document"])
                if item["document"].split("/")[0].casefold() in {".git", ".loop"}:
                    raise ContractError("Browser input cannot be a control file")
            for name in ("node", "playwright_module", "browser_executable"):
                if item[name] is not None and not Path(item[name]).is_absolute():
                    raise ContractError("Browser runtime paths must be explicit absolute paths")
            for step in item["steps"]:
                if not step["selector"].strip() or "\0" in step["selector"]:
                    raise ContractError("Invalid browser selector")
                if step["action"] in {"fill", "press", "assert_text", "assert_count"} and step["value"] is None:
                    raise ContractError("Browser action needs a declared value")
                if step["action"] == "assert_count" and (not step["value"].isascii() or not step["value"].isdigit()):
                    raise ContractError("Count assertion needs a nonnegative decimal value")
        else:
            if item["thresholds"]["min_successes"] > item["requests"]:
                raise ContractError("Load success threshold exceeds the declared workload")
        if item.get("url") and origin(item["url"]) not in item["allowed_origins"]:
            raise ContractError("Executor URL is outside explicitly authorized origins")


def configured_tools(root, profile=None):
    path = (profile or {}).get("execution_tools")
    manifest = safe_path(root, ".loop/scenario.json")
    if manifest.exists():
        path = load(manifest, "scenario").get("execution_tools", path)
    return load(safe_path(root, path), "execution-tools") if path else None


def input_identity(root, provision):
    result = {}
    for name in provision["inputs"]:
        path = safe_path(root, name)
        if not path.is_file() or path.stat().st_size > 10485760:
            raise ContractError("Provision input is absent or exceeds 10 MiB: " + name)
        result[name] = byte_digest(path.read_bytes())
    return canonical_digest(result)


def output_identity(root, provision, profile):
    root = root.resolve()
    roots = [safe_path(root, name) for name in provision["output_dirs"]]
    result, total = {}, 0
    for name, target in zip(provision["output_dirs"], roots):
        if not (matches(name, profile["snapshot"]["exclude"]) or matches(name + "/", profile["snapshot"]["exclude"])):
            raise ContractError("Provision output must be excluded from source snapshots: " + name)
        if target.exists() and not target.is_dir():
            raise ContractError("Provision output is not a directory: " + name)
        for directory, subdirs, names in os.walk(target, followlinks=False):
            for entry in [*subdirs, *names]:
                path = Path(directory) / entry
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    try:
                        resolved = path.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise ContractError("Invalid provision symlink: " + relative) from exc
                    if not any(resolved.is_relative_to(base) for base in roots):
                        raise ContractError("Dependency link escapes registered output directories: " + relative)
                    result[relative] = {"kind": "symlink", "target": resolved.relative_to(root).as_posix()}
                elif path.is_file():
                    size = path.stat().st_size
                    total += size
                    if total > provision["max_output_bytes"]:
                        raise ContractError("Provision outputs exceed the configured byte limit")
                    result[relative] = {"kind": "file", "size": size, "mode": path.stat().st_mode & 0o777,
                                        "sha256": byte_digest(path.read_bytes())}
                elif not path.is_dir():
                    raise ContractError("Unsupported dependency output: " + relative)
                else:
                    result[relative] = {"kind": "directory", "mode": path.stat().st_mode & 0o777}
                if len(result) > provision["max_output_files"]:
                    raise ContractError("Provision outputs exceed the configured file limit")
    return canonical_digest(result)


def dependency_identity(root, profile):
    config = configured_tools(root, profile)
    if not config or not config["provision"]["steps"]:
        return None
    return canonical_digest({"inputs": input_identity(root, config["provision"]),
                             "outputs": output_identity(root, config["provision"], profile)})


def tool_runtime_identity(root, profile):
    config = configured_tools(root, profile)
    if not config or not config["executors"]:
        return None
    from .contracts import ROOT
    paths = {sys.executable, str(ROOT / "scripts/execute_check.py"), str(ROOT / "scripts/browser_check.cjs"),
             str(ROOT / "loop_engineering/external_execution.py"), str(ROOT / "loop_engineering/execution_tools.py")}
    for item in config["executors"]:
        if item["kind"] == "browser":
            paths.update([item["node"], item["browser_executable"], str(Path(item["playwright_module"]) / "package.json")])
    identity = {}
    for name in sorted(paths):
        path = Path(name)
        if path.is_file():
            if path.stat().st_size > 268435456:
                raise ContractError("Executor runtime file exceeds identity limit")
            identity[name] = byte_digest(path.read_bytes())
        else:
            identity[name] = None
    return canonical_digest({"config": config, "runtime_files": identity})


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_load(plan, *, opener=None, clock=time.monotonic, sleep=time.sleep):
    """Bounded GET workload. Units and thresholds are integral and predeclared."""
    validate("execution-tools", {"schema_version": "1.0", "provision": {"inputs": [], "output_dirs": [], "steps": [],
                "max_output_files": 20000, "max_output_bytes": 268435456}, "executors": [plan]})
    if plan["kind"] != "http_load":
        raise ContractError("HTTP workload requires a registered HTTP load plan")
    opener = opener or build_opener(ProxyHandler({}), NoRedirects())
    start = clock(); deadline = start + plan["timeout_seconds"]
    lock = threading.Lock(); next_slot = start; next_index = 0; samples = []
    def work():
        nonlocal next_slot, next_index
        while True:
            with lock:
                if next_index >= plan["requests"] or clock() >= deadline:
                    return
                index = next_index; next_index += 1
                slot = max(clock(), next_slot); next_slot = slot + 1 / plan["requests_per_second"]
            if slot >= deadline:
                return
            sleep(max(0, slot - clock()))
            if clock() >= deadline:
                return
            before = clock(); status = None; count = 0; error = None
            try:
                request = Request(plan["url"], headers={"User-Agent": "loop-bounded-check/1"}, method="GET")
                try:
                    response = opener.open(request, timeout=min(plan["request_timeout_ms"] / 1000, deadline - before))
                except HTTPError as exc:
                    response = exc
                with response:
                    status = response.status
                    if origin(response.geturl()) != origin(plan["url"]):
                        raise ContractError("Load response changed its authorized origin")
                    count = len(response.read(plan["max_response_bytes"] + 1))
                    if count > plan["max_response_bytes"]:
                        error = "response_limit"
                    elif status != plan["expected_status"]:
                        error = "unexpected_status"
            except Exception as exc:
                error = type(exc).__name__
            sample = {"index": index, "status": status, "bytes": count, "latency_us": max(1, math.ceil((clock() - before) * 1000000)), "error": error}
            with lock:
                samples.append(sample)
    with ThreadPoolExecutor(max_workers=plan["concurrency"]) as pool:
        futures = [pool.submit(work) for _ in range(plan["concurrency"])]
        for future in futures:
            future.result()
    values = sorted(item["latency_us"] for item in samples)
    percentile = lambda p: values[max(0, math.ceil(len(values) * p / 100) - 1)] if values else None
    errors = sum(item["error"] is not None for item in samples)
    p95 = percentile(95)
    passed = (len(samples) == plan["requests"] and len(samples) - errors >= plan["thresholds"]["min_successes"]
              and errors <= plan["thresholds"]["max_errors"] and p95 is not None
              and p95 <= plan["thresholds"]["max_p95_ms"] * 1000)
    return {"result": "pass" if passed else "fail", "samples": sorted(samples, key=lambda item: item["index"]),
            "metrics": {"requests": len(samples), "successes": len(samples) - errors, "errors": errors,
                        "elapsed_ms": max(1, math.ceil((clock() - start) * 1000)),
                        "p50_us": percentile(50), "p95_us": p95, "p99_us": percentile(99)},
            "thresholds": plan["thresholds"], "workload": {key: plan[key] for key in
                ("requests", "concurrency", "requests_per_second", "expected_status", "max_response_bytes")}}
