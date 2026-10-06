"""Conservative setup discovery. Discovery never executes project commands."""

import json
import os
from pathlib import Path
import shutil
import tomllib

from .contracts import ROOT, ContractError, load, validate
from .workspace import safe_path

DEFAULT_EXCLUDES = [".git", ".git/**", ".loop", ".loop/**", "**/__pycache__/**",
                    "__pycache__/**", "*.pyc", ".venv/**", "venv/**", "node_modules/**",
                    "**/node_modules/**", ".pytest_cache/**", ".env", ".env.*", "**/.env", "**/.env.*"]


def local_capabilities() -> dict:
    return {"snapshot_capture": True, "bounded_execution": os.name == "posix", "durable_state": True,
            "run_cancellation": os.name == "posix", "isolated_workspace": False,
            "policy_enforcement": False, "trusted_evidence": False, "spend_accounting": False}


def command(argv: list[str], source: str, timeout: int = 300) -> dict:
    return {"argv": argv, "cwd": ".", "timeout_seconds": timeout, "source": source,
            "confirmed": False}


def discover(root: Path) -> dict:
    root = root.resolve()
    commands = {"build": None, "test": None, "lint": None, "start": None}
    languages = []
    notes = []
    if (root / "package.json").is_file():
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
        languages.append("typescript" if (root / "tsconfig.json").exists() else "javascript")
        for name in commands:
            script = name if name in scripts else "dev" if name == "start" and "dev" in scripts else None
            if script:
                commands[name] = command([manager, "run", script], "package.json scripts")
    python_paths = list((root / "tests").glob("test*.py")) if (root / "tests").is_dir() else []
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or python_paths:
        languages.append("python")
        config = tomllib.loads((root / "pyproject.toml").read_text()) if (root / "pyproject.toml").is_file() else {}
        if python_paths:
            unit = any("unittest" in path.read_text(encoding="utf-8", errors="replace") for path in python_paths[:20])
            commands["test"] = command(["python3", "-m", "unittest", "discover", "-s", "tests"] if unit
                                       else ["python3", "-m", "pytest"], "detected tests; review command")
        elif "pytest" in config.get("tool", {}):
            commands["test"] = command(["python3", "-m", "pytest"], "pyproject.toml pytest configuration")
    if (root / "go.mod").is_file():
        languages.append("go")
        commands.update(build=command(["go", "build", "./..."], "go.mod"),
                        test=command(["go", "test", "./..."], "go.mod"))
    if (root / "Cargo.toml").is_file():
        languages.append("rust")
        commands.update(build=command(["cargo", "check"], "Cargo.toml"),
                        test=command(["cargo", "test"], "Cargo.toml"))
    if not languages:
        languages = ["unknown"]
        notes.append("Configure commands and source context explicitly for this project.")
    notes.append("Detected commands are candidates; confirm them after review or execution.")
    context = [name for name in ("AGENTS.md", "README.md", "pyproject.toml", "package.json", "go.mod", "Cargo.toml")
               if (root / name).is_file() and not (root / name).is_symlink()]
    profile = {"schema_version": "0.2", "project_id": "local-project", "languages": sorted(set(languages)),
               "commands": commands, "conventions": [], "context_paths": context,
               "snapshot": {"exclude": DEFAULT_EXCLUDES, "max_file_bytes": 10485760,
                            "max_total_bytes": 104857600},
               "context_max_bytes": 60000, "notes": notes}
    validate("project", profile)
    return profile


def scaffold_task(profile: dict) -> dict:
    task = load(ROOT / "templates/task.json", "task")
    task.update(task_id="replace-with-task-id", objective="Replace this with the requested behavior.",
                non_goals=[], assumptions=[], dependencies=[], compatibility_requirements=[])
    task["scope"] = {"write_allow": ["**"], "write_deny": [".git", ".git/**", ".loop", ".loop/**"]}
    test = profile["commands"]["test"]
    task["criteria"] = [{"id": "behavior", "description": "Replace with an observable acceptance criterion.",
                          "check_ids": ["acceptance"]}]
    task["checks"] = [{"id": "acceptance", "type": "command", "description": "Replace with a meaningful behavioral check.",
                       "argv": test["argv"] if test else ["REPLACE_WITH_CHECK_COMMAND"],
                       "timeout_seconds": test["timeout_seconds"] if test else 60, "cwd": "."}]
    task["extensions"] = {"scaffold": True}
    return task


def initialize(root: Path, *, task_file: Path | None = None, native: bool = False,
               scenario: str | None = None, spec_file: Path | None = None) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ContractError("Project directory must already exist")
    if spec_file is not None and scenario is None:
        raise ContractError("--spec requires --scenario")
    if scenario is not None and (native or (root / ".loop/engine.json").exists()):
        raise ContractError("Scenario presets currently use a coding host; initialize the native single-task route separately")
    control = safe_path(root, ".loop")
    profile_path = safe_path(root, ".loop/project.json")
    created, preserved, updated = [], [], []
    profile = load(profile_path, "project") if profile_path.exists() else discover(root)
    if scenario is not None and not profile_path.exists():
        from .scenarios import initial_context_limit
        profile["context_max_bytes"] = initial_context_limit(scenario)
    task = load(task_file, "task") if task_file else scaffold_task(profile)
    if scenario is not None and task_file is None:
        task.update(kind="feature", workflow="standard")
    files = {profile_path: json.dumps(profile, indent=2) + "\n",
             control / "task.json": json.dumps(task, indent=2) + "\n",
             control / "handoff.md": (ROOT / "templates/handoff.md").read_text(),
             control / "agent-instructions.md": (ROOT / "templates/agent-instructions.md").read_text(),
             root / "LOOP.md": (ROOT / "LOOP.md").read_text()}
    if native:
        files[control / "engine.json"] = (ROOT / "templates/engine.json").read_text()
    if scenario is not None:
        from .scenarios import setup_files
        files.update(setup_files(root, scenario, spec_file))
    for path in files:
        if path.is_symlink():
            raise ContractError(f"Refusing to write through a symlink: {path.name}")
        safe_path(root, path.relative_to(root).as_posix())
        if path.exists() and not path.is_file():
            raise ContractError(f"Initialization target must be a regular file: {path.relative_to(root)}")
    for path, content in files.items():
        if path.exists():
            if spec_file is not None and path == control / "spec.md" and not path.read_text(encoding="utf-8").strip():
                from .workspace import atomic_write
                atomic_write(path, content.encode("utf-8"))
                updated.append(str(path.relative_to(root)))
            else:
                preserved.append(str(path.relative_to(root)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            created.append(str(path.relative_to(root)))
    result = {"created": created, "preserved": preserved, "updated": updated,
              "next": "Review .loop/project.json and replace the task scaffold; then run doctor."}
    if scenario is not None:
        result.update(scenario=scenario, execution="host_orchestrated", entrypoint=str(control / "start.md"),
                      next="Open this project in your coding host and say: Read .loop/start.md and develop the supplied spec; ask me only material questions.")
        if not (control / "spec.md").read_text(encoding="utf-8").strip():
            result.update(entrypoint=str(control / "setup.md"),
                          next="Read .loop/setup.md with your coding host, or run setup with --adapter and --state-dir; the requirements agent collects a spec from docs and actual answers.")
    return result


def diagnose(root: Path, *, probe_runtime: bool = False) -> dict:
    root = root.resolve()
    errors, warnings = [], []
    data = {}
    for name, kind in (("project.json", "project"), ("task.json", "task")):
        path = root / ".loop" / name
        try:
            data[kind] = load(path, kind)
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    profile = data.get("project")
    if profile:
        for name, item in profile["commands"].items():
            if item:
                executable = item["argv"][0]
                found = executable_available(root, item["cwd"], executable)
                if not found:
                    warnings.append(f"{name}: executable unavailable: {executable}")
                if not item["confirmed"]:
                    warnings.append(f"{name}: detected command needs review/confirmation")
        if not profile["commands"]["test"]:
            warnings.append("No project-wide test command detected")
        for path in profile["context_paths"]:
            if not (root / path).is_file():
                warnings.append(f"Context path is missing: {path}")
    task = data.get("task")
    scenario_report = None
    from .scenarios import configured, status as scenario_status
    if configured(root):
        try:
            scenario_report = scenario_status(root, task)
        except (OSError, ValueError) as exc:
            errors.append("scenario: " + str(exc))
    engine = None
    engine_path = root / ".loop/engine.json"
    if engine_path.is_file():
        from .native_contracts import load_engine
        try:
            engine = load_engine(engine_path)
        except (OSError, ValueError) as exc:
            errors.append("engine.json: " + str(exc))
    if task:
        scenario_scaffold = scenario_report is not None and task.get("extensions", {}).get("scaffold")
        if task.get("extensions", {}).get("scaffold") and not scenario_scaffold:
            errors.append("Replace the scaffold's objective/criteria/check and remove extensions.scaffold before starting")
        for check in task["checks"]:
            if not scenario_scaffold and check["type"] == "command" and not (engine and engine["runtime"]["backend"] == "docker") and not executable_available(root, check.get("cwd", "."), check["argv"][0]):
                errors.append(f"Required check executable unavailable: {check['argv'][0]}")
        if not engine and (task["limits"]["max_tokens"] is not None or task["limits"]["max_cost_microunits"] is not None):
            errors.append("Local controller cannot reserve hard token/cost budgets; use a supported accounting runtime")
        if not engine and task["autonomy"] == "unattended":
            errors.append("Local controller does not provide unattended isolation or policy enforcement")
        elif not engine and task["autonomy"] == "assisted":
            warnings.append("Effective autonomy is manual/operator-supervised because the collector shares OS authority")
        missing = [name for name in task["required_capabilities"]["runtime"] if not local_capabilities().get(name)] if not engine else []
        if missing:
            errors.append("Required runtime capabilities unavailable: " + ", ".join(missing))
        if not engine and any(check["type"] != "command" for check in task["checks"]):
            warnings.append("Required non-command checks need an external evaluator; local verification will block")
        reserve = max(task["limits"].get("verification_reserve_seconds", 0), sum(check.get("timeout_seconds", 0) for check in task["checks"]))
        if reserve >= task["limits"]["max_wall_seconds"]:
            warnings.append("Verification reserve consumes the entire wall budget; implementation cannot start")
    result = {"ok": not errors, "effective_autonomy": "manual", "errors": errors, "warnings": warnings,
              "capabilities": local_capabilities()}
    if scenario_report is not None:
        result["scenario"] = scenario_report
        result["ready_to_start"] = not errors and scenario_report["ready_for_controller"]
        if not scenario_report["ready_for_controller"]:
            warnings.append("Scenario setup is valid; the host coordinator must finish intake and prepare the task before controller start")
    if engine:
        from .models import ModelDriver
        from .stages import validate_stages
        import tempfile
        from .runtime import runtime_for
        try:
            if task:
                validate_stages(task, engine["stages"])
                hard = task["limits"]["max_tokens"] is not None or task["limits"]["max_cost_microunits"] is not None
                if hard and engine["model"]["provider"] == "anthropic":
                    errors.append("Anthropic input counting is an estimate; this driver refuses hard spend caps")
                if task["limits"]["max_cost_microunits"] is not None and engine["model"]["pricing"] is None:
                    errors.append("A hard cost cap needs explicit conservative pricing configuration")
                caps = ModelDriver(engine["model"], engine["response_limits"], None).capabilities
                if any(caps.get(c) is not True for c in task["required_capabilities"]["agent"]):
                    errors.append("Native model driver lacks a required agent capability")
            if engine["model"]["model"] == "set-your-model-id":
                warnings.append("Select an explicit provider model ID before any native dispatch")
            result["native"] = {"configured": True, "runtime_verified": False, "model_provider": engine["model"]["provider"]}
            if probe_runtime:
                with tempfile.TemporaryDirectory(prefix="loop-runtime-probe-") as directory:
                    observed = runtime_for(engine["runtime"]).probe(Path(directory))
                result["native"]["runtime_probe"] = observed
                result["native"]["runtime_verified"] = observed["verified"]
                result["capabilities"] = observed["capabilities"]
                if task and task["autonomy"] == "unattended" and observed["verified"] is not True:
                    errors.append("Required unattended runtime conformance did not pass")
                if task and any(observed["capabilities"].get(c) is not True for c in task["required_capabilities"]["runtime"]):
                    errors.append("Probed runtime lacks a required capability")
                if observed["verified"] is True and task:
                    result["effective_autonomy"] = task["autonomy"]
            elif engine["runtime"]["backend"] == "docker":
                warnings.append("Run doctor --probe-runtime to verify containment; configuration alone grants no capability")
        except (OSError, ValueError) as exc:
            errors.append("Native setup: " + str(exc))
        result["ok"] = not errors
    if scenario_report is not None:
        result["ready_to_start"] = result["ok"] and scenario_report["ready_for_controller"]
    return result


def executable_available(root: Path, cwd: str, executable: str) -> bool:
    if "/" not in executable:
        return shutil.which(executable) is not None
    path = Path(executable) if Path(executable).is_absolute() else root / cwd / executable
    return path.is_file() and os.access(path, os.X_OK)
