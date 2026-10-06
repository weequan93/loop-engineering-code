"""Repository-local CLI. Every process dispatch is an explicit operator action."""

import argparse
import json
from pathlib import Path
import sys
from . import __version__

from .adapters import CodexDriver, CommandDriver, drive
from .contracts import ContractError, load
from .controller import Controller
from .project import diagnose, initialize
from .store import Store
from .native_engine import NativeController
from .native_contracts import load_engine
from .budgets import Ledger
from .evaluators import sign_result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="loop", description="Portable coding workflow and native loop controller")
    root.add_argument("--version", action="version", version="loop-engineering " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Discover commands and create missing templates without overwriting files")
    init.add_argument("project", type=Path)
    init.add_argument("--task", type=Path)
    init.add_argument("--native", action="store_true", help="Also create missing native engine configuration")
    init.add_argument("--scenario", help="Prepare a host-orchestrated preset; see scenarios")
    init.add_argument("--spec", type=Path, help="UTF-8 requirements file for the selected scenario")
    reinit = commands.add_parser("reinit", help="Archive old .loop settings and initialize a fresh scenario; preserve source and history")
    reinit.add_argument("project", type=Path)
    reinit.add_argument("--scenario", default="development")
    reinit.add_argument("--spec", type=Path, required=True)
    reinit.add_argument("--state-dir", type=Path, required=True, help="Existing private state; cancel unfinished teams first")
    commands.add_parser("scenarios", help="List implemented scenario presets without dispatching agents")
    scenario_context = commands.add_parser("scenario-context", help="Export the spec, role instructions and planning state for an existing coding host")
    scenario_context.add_argument("project", type=Path)
    scenario_context.add_argument("--role", help="Role ID; defaults to the scenario coordinator")
    scenario_context.add_argument("--output", type=Path)
    team_start = commands.add_parser("team-start", help="Freeze a prepared team batch without dispatching workers")
    team_start.add_argument("project", type=Path)
    team_start.add_argument("--plan", type=Path)
    team_start.add_argument("--state-dir", type=Path, required=True)
    team_runs = commands.add_parser("team-runs", help="Find durable team runs, including interrupted starts")
    team_runs.add_argument("--state-dir", type=Path, required=True)
    team_memory = commands.add_parser("team-memory", help="Inspect bounded source-linked role memory without model dispatch")
    team_memory.add_argument("team_id")
    team_memory.add_argument("--state-dir", type=Path, required=True)
    team_memory.add_argument("--role", default="coordinator")
    team_memory.add_argument("--task")
    team_memory.add_argument("--max-bytes", type=int)
    automatic = commands.add_parser("team-run", help="Continuously coordinate, execute and rework a supervised team")
    automatic.add_argument("project", type=Path, nargs="?")
    automatic.add_argument("--team-id", help="Continue the same team and cumulative budget")
    automatic.add_argument("--policy", type=Path)
    automatic.add_argument("--state-dir", type=Path, required=True)
    automatic.add_argument("--adapter", choices=("command", "codex", "provider", "desktop"), required=True)
    automatic.add_argument("--review-adapter", choices=("codex",), help="Desktop-only separate independent reviewer")
    automatic.add_argument("--argv")
    automatic.add_argument("--model")
    automatic.add_argument("--engine", type=Path)
    automatic.add_argument("--max-cycles", type=int, default=100)
    setup = commands.add_parser("setup", help="Call the requirements agent to collect a spec; without --adapter export portable instructions only")
    setup.add_argument("project", type=Path, nargs="?")
    setup.add_argument("--team-id", help="Continue the same requirements setup and cumulative budget")
    setup.add_argument("--state-dir", type=Path)
    setup.add_argument("--adapter", choices=("command", "codex", "provider", "desktop"))
    setup.add_argument("--argv")
    setup.add_argument("--model")
    setup.add_argument("--engine", type=Path)
    setup.add_argument("--brief", help="An actual user idea or initial requirements; optional when docs exist")
    setup.add_argument("--policy", type=Path)
    setup.add_argument("--max-cycles", type=int, default=100)
    setup.set_defaults(review_adapter=None)
    desktop_config = commands.add_parser("desktop-config", help="Export a scoped Claude Desktop MCP entry without editing host settings")
    desktop_config.add_argument("project", type=Path)
    desktop_config.add_argument("--state-dir", type=Path, required=True)
    desktop_config.add_argument("--output", type=Path)
    for name in ("host-config", "host-install"):
        host = commands.add_parser(name, help="Connect native App/CLI coordination with project-scoped Loop tools; no model dispatch")
        host.add_argument("project", type=Path)
        host.add_argument("--state-dir", type=Path, required=True)
        host.add_argument("--host", choices=("codex", "claude-code", "claude-desktop"), default="codex")
        if name == "host-config":
            host.add_argument("--output", type=Path, help="Export JSON with the scoped entry and configuration text without editing host settings")
    for name in ("host-supervise", "host-supervisor-run", "host-supervisor-status",
                 "host-supervisor-control", "host-supervisor-reconcile", "host-supervisor-runtime"):
        supervisor = commands.add_parser(name, help="Explicit persistent Codex coordination; original team/evidence/limits retained")
        supervisor.add_argument("project", type=Path)
        supervisor.add_argument("--state-dir", type=Path, required=True)
        if name == "host-supervise":
            supervisor.add_argument("--team-id", required=True)
            supervisor.add_argument("--host-idle", action="store_true", help="Confirm original project writers stopped before takeover")
            supervisor.add_argument("--foreground", action="store_true", help="Run in this terminal instead of a detached supervisor")
            supervisor.add_argument("--executable", default="codex")
            supervisor.add_argument("--model")
            supervisor.add_argument("--max-wall-seconds", type=int, default=28800)
            supervisor.add_argument("--max-turns", type=int, default=100)
            supervisor.add_argument("--turn-timeout-seconds", type=int, default=14400)
            supervisor.add_argument("--poll-seconds", type=int, default=5)
            supervisor.add_argument("--approval-mode", choices=("never", "auto-review"), default="never")
        else:
            supervisor.add_argument("--supervisor-id", required=True)
        if name == "host-supervisor-control":
            supervisor.add_argument("--action", choices=("pause", "resume", "cancel"), required=True)
        if name == "host-supervisor-reconcile":
            supervisor.add_argument("--note", required=True, help="Actual inspection of stopped host writers and retained effects")
            supervisor.add_argument("--continue", dest="continue_work", action="store_true", help="Launch after successful actual reconciliation")
        if name == "host-supervisor-runtime":
            supervisor.add_argument("--update-id", required=True)
            supervisor.add_argument("--expected-revision", type=int, required=True)
            supervisor.add_argument("--expected-candidate", required=True)
            supervisor.add_argument("--approval-mode", choices=("never", "auto-review"), required=True)
            supervisor.add_argument("--authorization", required=True, help="Actual operator authorization for this guarded runtime route")
            supervisor.add_argument("--host-idle", action="store_true")
    repair = commands.add_parser("host-plan-repair", help="Journal an empty-stage repair for unstarted native tasks; preserve accepted work and budgets")
    repair.add_argument("project", type=Path)
    repair.add_argument("--team-id", required=True)
    repair.add_argument("--state-dir", type=Path, required=True)
    repair.add_argument("--dry-run", action="store_true")
    for name in ("host-review-register", "host-review-status"):
        review = commands.add_parser(name, help="Configure or inspect an independent native review executor; registration dispatches no model")
        review.add_argument("project", type=Path)
        review.add_argument("--state-dir", type=Path, required=True)
        if name == "host-review-register":
            review.add_argument("--adapter", choices=("codex",), required=True)
            review.add_argument("--executable", default="codex")
            review.add_argument("--model")
            review.add_argument("--timeout-seconds", type=int, default=180)
            review.add_argument("--max-attempts", type=int, default=3)
    for name in ("team-answer", "team-reconcile"):
        command = commands.add_parser(name)
        command.add_argument("team_id")
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "team-answer":
            command.add_argument("--question", required=True)
            command.add_argument("--answer", required=True)
        else:
            command.add_argument("--operation-id", required=True)
            command.add_argument("--note", required=True)
    for name in ("team-status", "team-request", "team-submit", "team-drive", "team-collect",
                 "team-receive", "team-pause", "team-resume", "team-cancel", "team-audit"):
        command = commands.add_parser(name)
        command.add_argument("team_id")
        command.add_argument("--state-dir", type=Path, required=True)
        if name in {"team-request", "team-submit", "team-drive", "team-collect", "team-receive"}:
            command.add_argument("--task", required=True, help="Task ID in the prepared team plan")
        if name == "team-request":
            command.add_argument("--output", type=Path)
        if name == "team-submit":
            command.add_argument("--request-id", required=True)
            command.add_argument("--file", type=Path, required=True)
        if name == "team-drive":
            command.add_argument("--adapter", choices=("command", "codex"), required=True)
            command.add_argument("--argv", help="JSON argument array for a supervised proposal command")
            command.add_argument("--model")
            command.add_argument("--timeout", type=int, default=180)
        if name == "team-receive":
            command.add_argument("--handoff-id", required=True)
            command.add_argument("--role", required=True)
            command.add_argument("--decision", choices=("accept", "reject"), required=True)
            command.add_argument("--note", required=True)
    doctor = commands.add_parser("doctor", help="Validate setup; optional --probe-runtime runs deterministic containment probes")
    doctor.add_argument("project", type=Path)
    doctor.add_argument("--probe-runtime", action="store_true", help="Explicitly run deterministic runtime containment probes")
    start = commands.add_parser("start", help="Freeze contract/profile, capture baseline, and create a durable run")
    start.add_argument("project", type=Path)
    start.add_argument("--task", type=Path)
    start.add_argument("--profile", type=Path)
    start.add_argument("--engine", type=Path, help="Native engine configuration; defaults to .loop/engine.json when present")
    start.add_argument("--no-baseline", action="store_true")
    start.add_argument("--state-dir", type=Path, required=True)
    runs = commands.add_parser("runs", help="List saved runs, including interrupted starts")
    runs.add_argument("--state-dir", type=Path, required=True)
    for name, help_text in (
        ("context", "Export a fresh bounded task/context bundle"),
        ("step", "Validate and apply one agent proposal, then verify"),
        ("verify", "Run all required executable checks on a fresh copy"),
        ("resume", "Reconcile interrupted work without resetting budgets"),
        ("reconcile", "Attest that a specific unknown process effect has been inspected/stopped"),
        ("status", "Inspect current state, usage, and gate decision"),
        ("cancel", "Request cancellation; the active broker polls this flag"),
        ("restore", "Materialize the saved checkpoint into a new directory"),
        ("drive", "Run a bounded configured agent adapter"),
        ("drive-native", "Explicitly dispatch a reserved direct provider response"),
        ("pause", "Request a recoverable pause without discarding work"),
        ("amend", "Accept an explicit next task revision without resetting cumulative budgets"),
        ("model-switch", "Change native provider/model while retaining task and budgets"),
        ("evaluation-request", "Export a snapshot-bound procedure for a registered evaluator"),
        ("evaluation-import", "Validate an authenticated evaluator result and immutable artifacts"),
        ("evaluation-run", "Execute an explicitly registered supervised browser or HTTP check"),
        ("audit", "Authenticate and reconstruct the event checkpoint stream without changing state"),
        ("rebuild", "Repair a projection from verified replay; preserves unresolved effects"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run_id")
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "step":
            command.add_argument("--file", type=Path, required=True)
        if name == "reconcile":
            command.add_argument("--action-id", required=True)
            command.add_argument("--note", required=True)
        if name == "context":
            command.add_argument("--output", type=Path)
        if name == "restore":
            command.add_argument("--target", type=Path, required=True)
        if name == "status":
            command.add_argument("--events", action="store_true")
        if name == "drive":
            command.add_argument("--adapter", choices=("codex", "command"), required=True)
            command.add_argument("--argv", help="JSON argument array for a generic command adapter")
            command.add_argument("--model", help="Explicit Codex model override; otherwise the isolated CLI default")
            command.add_argument("--turns", type=int, default=1)
            command.add_argument("--timeout", type=int, default=180)
        if name == "drive-native":
            command.add_argument("--turns", type=int, default=1)
        if name == "pause":
            command.add_argument("--reason", default="Operator requested a pause")
        if name == "amend":
            command.add_argument("--task", type=Path, required=True)
            command.add_argument("--note", required=True)
        if name == "model-switch":
            command.add_argument("--file", type=Path, required=True, help="JSON model configuration object")
        if name in {"evaluation-request", "evaluation-run"}:
            command.add_argument("--check-id", required=True)
        if name == "evaluation-request":
            command.add_argument("--output", type=Path, required=True)
        if name == "evaluation-import":
            command.add_argument("--file", type=Path, required=True)
    for name in ("key-create", "key-revoke"):
        command = commands.add_parser(name, help="Manage a host-owned evaluator key")
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--key-id", required=True)
        if name == "key-create":
            command.add_argument("--role", choices=("reviewer", "human", "interaction", "artifact"), required=True)
    signer = commands.add_parser("evaluation-sign", help="Registered evaluator attests a performed procedure and real artifacts")
    signer.add_argument("--state-dir", type=Path, required=True)
    signer.add_argument("--request", type=Path, required=True)
    signer.add_argument("--key-id", required=True)
    signer.add_argument("--result", choices=("pass", "fail", "inconclusive"), required=True)
    signer.add_argument("--summary", required=True)
    signer.add_argument("--artifacts", type=Path, nargs="+", required=True)
    signer.add_argument("--findings", type=Path)
    signer.add_argument("--output", type=Path, required=True)
    return root


def summary(data: dict) -> dict:
    result = {"run_id": data["run_id"], "state": data["state"], "last_gate": data["last_gate"],
            "last_recovery": data["last_recovery"], "known_tokens": data["known_tokens"],
            "usage_complete": data["usage_complete"], "evidence_records": data["selected_evidence"]}
    result["provisioning"] = data.get("provisioning")
    result["pending_process"] = data.get("pending_process")
    if "native" in data:
        result["native"] = {"runtime": data["native"]["runtime_probe"], "budget": Ledger(data["native"]["reservations"]).balance(),
                            "stages": data["native"]["stage_status"], "model": {k: data["native"]["current_model"][k] for k in ("provider", "model")}}
        result["native"]["execution_attempts"] = data["native"].get("execution_attempts", {})
    return result


def controller_for(store: Store, run_id: str):
    data = store.get(run_id)
    if "native" in data:
        return NativeController(store, Path(data["native"]["config_path"]), probe=data["native"]["runtime_probe"])
    return Controller(store)


def write_new_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def team_command(args, store):
    from .team_engine import TeamController
    team = TeamController(store)
    command = args.command
    if command in {"team-run", "setup", "team-answer", "team-reconcile"}:
        from .team_automation import TeamAutomation
        from .team_host import TeamHost
        if command in {"team-run", "setup"}:
            if bool(args.project) == bool(args.team_id):
                raise ContractError("Provide a project for a new team or --team-id for continuation")
            if args.adapter == "command" and not args.argv or args.adapter == "provider" and not args.engine:
                raise ContractError("Command needs --argv; provider needs --engine")
            host = TeamHost(team, driver=CommandDriver(json.loads(args.argv)) if args.adapter == "command" else None,
                            engine=args.engine if args.adapter == "provider" else None, codex=args.adapter == "codex", model=args.model,
                            desktop=args.adapter == "desktop", review_codex=args.review_adapter == "codex")
            automation = TeamAutomation(team, host)
            if args.team_id and args.policy and load(args.policy, "team-policy") != team.teams.get(args.team_id)["automation"]["policy"]:
                raise ContractError("Team policy is frozen; continuation cannot reset limits")
            if command == "setup" and args.team_id:
                automation.begin_spec(args.team_id, brief=args.brief)
                if team.teams.get(args.team_id).get("stage") not in {"SPEC", "INTAKE"}:
                    raise ContractError("This team has begun development; use team-run")
            team_id = args.team_id or automation.start(args.project, args.policy,
                specification=command == "setup", brief=args.brief if command == "setup" else None)["team_id"]
            return automation.run(team_id, max_cycles=args.max_cycles, setup_only=command == "setup")
        automation = TeamAutomation(team, None)
        if command == "team-answer":
            return automation.answer(args.team_id, args.question, args.answer)
        return automation.reconcile(args.team_id, args.operation_id, args.note)
    if command == "team-start":
        return team.start(args.project, args.plan)
    if command == "team-runs":
        return {"teams": team.teams.runs()}
    if command == "team-status":
        return team.status(args.team_id)
    if command == "team-memory":
        return team.memory(args.team_id, role=args.role, task_id=args.task, max_bytes=args.max_bytes)
    if command == "team-audit":
        data = team.teams.audit(args.team_id)
        return {"team_id": args.team_id, "authenticated": True, "revision": data["revision"], "status": data["status"]}
    if command == "team-request":
        result = team.request(args.team_id, args.task)
        if args.output:
            write_new_json(args.output, result)
            return {"context_file": str(args.output.resolve()), "request_id": result["team_assignment"]["request_id"]}
        return result
    if command == "team-submit":
        return team.submit(args.team_id, args.task, args.request_id, load(args.file, "step"))
    if command == "team-drive":
        if args.adapter == "command" and not args.argv:
            raise ContractError("A command adapter requires --argv as a JSON array")
        driver = CodexDriver(model=args.model) if args.adapter == "codex" else CommandDriver(json.loads(args.argv))
        return team.drive(args.team_id, args.task, driver, timeout=args.timeout)
    if command == "team-collect":
        return team.collect(args.team_id, args.task)
    if command == "team-receive":
        return team.receive(args.team_id, args.task, args.handoff_id, args.role,
                            accept=args.decision == "accept", note=args.note)
    if command == "team-resume":
        return team.resume(args.team_id)
    return team.stop(args.team_id, cancel=command == "team-cancel")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.project, task_file=args.task, native=args.native,
                                scenario=args.scenario, spec_file=args.spec)
        elif args.command == "reinit":
            from .setup import reinitialize
            result = reinitialize(args.project, args.spec, Store(args.state_dir), scenario=args.scenario)
        elif args.command == "setup":
            if bool(args.project) == bool(args.team_id):
                raise ContractError("Provide a project for setup or --team-id for continuation")
            if args.adapter is None:
                if args.team_id or args.brief is not None or args.argv or args.engine or args.model or args.policy:
                    raise ContractError("Setup options require an explicitly selected --adapter and --state-dir")
                result = initialize(args.project, scenario="development")
                result.update(role="requirements_reviewer", entrypoint=str(args.project.resolve() / ".loop/setup.md"),
                    automatic_dispatch=False, next="Open .loop/setup.md in your coding host to collect requirements from docs and actual answers; or call setup with --adapter and --state-dir.")
            else:
                if args.state_dir is None:
                    raise ContractError("Requirements agent dispatch needs --state-dir outside the project")
                if args.project:
                    if args.state_dir.resolve().is_relative_to(args.project.resolve()):
                        raise ContractError("Place --state-dir outside the project workspace")
                    from .team_engine import TeamController
                    active = TeamController(Store(args.state_dir)).teams.runs()
                    if any(row["workspace"] == str(args.project.resolve()) and row["status"] not in {"COMPLETE", "CANCELLED"} for row in active):
                        raise ContractError("An unfinished team owns this project; continue its --team-id before changing setup")
                    initialize(args.project, scenario="development")
                result = team_command(args, Store(args.state_dir))
        elif args.command == "desktop-config":
            from .desktop_bridge import desktop_config
            result = desktop_config(args.project, args.state_dir)
            if args.output:
                write_new_json(args.output, result)
                result = {"configuration_file": str(args.output.resolve()), "host_settings_modified": False}
        elif args.command in {"host-config", "host-install"}:
            from .host_install import configuration, install
            result = (install if args.command == "host-install" else configuration)(args.project, args.state_dir, args.host)
            if args.command == "host-config" and args.output:
                write_new_json(args.output, result)
                result = {"configuration_file": str(args.output.resolve()), "host_settings_modified": False,
                          "automatic_dispatch": False}
        elif args.command in {"host-supervise", "host-supervisor-run", "host-supervisor-status",
                              "host-supervisor-control", "host-supervisor-reconcile", "host-supervisor-runtime"}:
            from .native_host import NativeHostService
            from .native_supervisor import NativeSupervisor
            service = NativeHostService(args.project, Store(args.state_dir))
            try:
                supervisor = NativeSupervisor(service)
                if args.command == "host-supervise":
                    data = supervisor.create(args.team_id, host_idle_confirmed=args.host_idle,
                        max_wall_seconds=args.max_wall_seconds, max_turns=args.max_turns,
                        turn_timeout_seconds=args.turn_timeout_seconds, poll_seconds=args.poll_seconds,
                        executable=args.executable, model=args.model, approval_mode=args.approval_mode)
                    result = supervisor.run(data["id"]) if args.foreground else supervisor.launch(data["id"])
                elif args.command == "host-supervisor-status":
                    result = supervisor.status(args.supervisor_id)
                elif args.command == "host-supervisor-run":
                    result = supervisor.run(args.supervisor_id)
                elif args.command == "host-supervisor-reconcile":
                    result = supervisor.reconcile(args.supervisor_id, args.note)
                    if args.continue_work:
                        result = supervisor.launch(args.supervisor_id)
                elif args.command == "host-supervisor-runtime":
                    result = supervisor.runtime_update(args.supervisor_id, update_id=args.update_id,
                        expected_revision=args.expected_revision, expected_candidate=args.expected_candidate,
                        approval_mode=args.approval_mode, authorization=args.authorization,
                        host_idle_confirmed=args.host_idle)
                else:
                    result = supervisor.control(args.supervisor_id, args.action)
                    if args.action == "resume":
                        result = supervisor.launch(args.supervisor_id)
                if result.get("status") == "RECONNECT_REQUIRED":
                    result = supervisor.journal.get(result["id"])
            finally:
                service.close()
            if result.get("status") == "RECONNECT_REQUIRED":
                from .native_supervisor import reconnect_supervisor
                reconnect_supervisor(result, args.project, args.state_dir)
        elif args.command == "host-plan-repair":
            from .native_host import NativeHostService
            if args.state_dir.resolve().is_relative_to(args.project.resolve()):
                raise ContractError("Plan repair requires private state outside the project")
            service = NativeHostService(args.project, Store(args.state_dir))
            try:
                result = service.repair_plan(args.team_id, dry_run=args.dry_run)
            finally:
                service.close()
        elif args.command in {"host-review-register", "host-review-status"}:
            from .native_review import register, registration
            if args.state_dir.resolve().is_relative_to(args.project.resolve()):
                raise ContractError("Native review registration requires private state outside the project")
            store = Store(args.state_dir)
            if args.command == "host-review-register":
                result = register(store, args.project, adapter=args.adapter, executable=args.executable,
                    model=args.model, timeout_seconds=args.timeout_seconds, max_attempts=args.max_attempts)
            else:
                result = {"project": str(args.project.resolve()), "registration": registration(store, args.project),
                          "model_dispatched": False}
        elif args.command == "scenarios":
            from .scenarios import presets
            result = {"scenarios": presets(), "automatic_dispatch": False}
        elif args.command == "scenario-context":
            from .scenarios import context
            result = context(args.project, args.role)
            if args.output:
                write_new_json(args.output, result)
                result = {"context_file": str(args.output.resolve()), "mode": "host_orchestrated"}
        elif args.command == "doctor":
            result = diagnose(args.project, probe_runtime=args.probe_runtime)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        else:
            if args.command in {"start", "team-start", "team-run"} and args.project and args.state_dir.resolve().is_relative_to(args.project.resolve()):
                raise ContractError("Place --state-dir outside the project workspace")
            store = Store(args.state_dir)
            controller = Controller(store)
            if args.command.startswith("team-"):
                result = team_command(args, store)
            elif args.command == "key-create":
                store.authorities.create(args.key_id, args.role)
                result = {"key_id": args.key_id, "role": args.role, "created": True}
            elif args.command == "key-revoke":
                if args.key_id in {"controller", "collector"}:
                    raise ContractError("Built-in journal/collector authorities require an audited key migration")
                store.authorities.revoke(args.key_id)
                result = {"key_id": args.key_id, "revoked": True}
            elif args.command == "evaluation-sign":
                signed = sign_result(store.authorities, load(args.request), args.key_id, result=args.result,
                                     summary=args.summary, artifacts=args.artifacts,
                                     findings=load(args.findings) if args.findings else [])
                write_new_json(args.output, signed)
                result = {"signed_result": str(args.output.resolve())}
            elif args.command == "runs":
                result = {"runs": store.runs()}
            elif args.command == "start":
                engine_path = args.engine or args.project / ".loop/engine.json"
                if args.engine or engine_path.is_file():
                    controller = NativeController(store, engine_path)
                result = summary(controller.start(args.project, args.task or args.project / ".loop/task.json",
                                                  args.profile or args.project / ".loop/project.json", baseline=not args.no_baseline))
            elif args.command in {"audit", "rebuild"}:
                data = store.rebuild(args.run_id) if args.command == "rebuild" else store.replay(args.run_id)
                result = {"run_id": args.run_id, "authenticated": True, "event_sequence": data["state"]["event_sequence"],
                          "status": data["state"]["status"], "projection_rebuilt": args.command == "rebuild"}
            else:
                controller = controller_for(store, args.run_id)
                if args.command in {"drive-native", "amend", "model-switch", "evaluation-request", "evaluation-import", "evaluation-run"}:
                    if not isinstance(controller, NativeController):
                        raise ContractError("This operation requires a native run started with --engine")
                if args.command == "drive-native":
                    result = summary(controller.drive(args.run_id, turns=args.turns))
                elif args.command == "evaluation-run":
                    result = controller.execute_evaluation(args.run_id, args.check_id)
                elif args.command == "pause":
                    result = summary(controller.pause(args.run_id, args.reason))
                elif args.command == "amend":
                    result = summary(controller.amend(args.run_id, args.task, args.note))
                elif args.command == "model-switch":
                    result = summary(controller.switch_model(args.run_id, load(args.file)))
                elif args.command == "evaluation-request":
                    write_new_json(args.output, controller.evaluation_request(args.run_id, args.check_id))
                    result = {"evaluation_request": str(args.output.resolve())}
                elif args.command == "evaluation-import":
                    result = summary(controller.import_evaluation(args.run_id, load(args.file)))
                else:
                    result = None
            if args.command == "context":
                result = controller.context(args.run_id)
                if args.output:
                    with args.output.open("x", encoding="utf-8") as handle:
                        json.dump(result, handle, indent=2, ensure_ascii=False)
                        handle.write("\n")
                    result = {"context_file": str(args.output.resolve())}
            elif args.command == "step":
                result = summary(controller.submit(args.run_id, load(args.file, "step")))
            elif args.command == "verify":
                result = summary(controller.verify(args.run_id))
            elif args.command == "resume":
                result = summary(controller.resume(args.run_id))
            elif args.command == "reconcile":
                result = summary(controller.reconcile_process(args.run_id, args.action_id, args.note))
            elif args.command == "cancel":
                store.cancel(args.run_id)
                result = {"run_id": args.run_id, "cancellation_requested": True}
            elif args.command == "restore":
                data = store.get(args.run_id)
                if data["checkpoint_snapshot"] is None:
                    raise ContractError("Run has no recoverable code checkpoint")
                controller.snapshots.materialize(data["checkpoint_snapshot"], args.target)
                result = {"restored_to": str(args.target.resolve()), "snapshot_digest": data["checkpoint_snapshot"]["digest"]}
            elif args.command == "drive":
                if isinstance(controller, NativeController):
                    raise ContractError("Use drive-native or a manual file proposal for native runs")
                if args.adapter == "command" and not args.argv:
                    raise ContractError("A command adapter requires --argv as a JSON array")
                driver = CodexDriver(model=args.model) if args.adapter == "codex" else CommandDriver(json.loads(args.argv))
                result = summary(drive(controller, args.run_id, driver, turns=args.turns, timeout=args.timeout))
            elif args.command == "status":
                data = store.get(args.run_id)
                result = summary(data)
                result["workspace_matches_checkpoint"] = controller.snapshots.capture(Path(data["workspace"]), data["profile"])["digest"] == data["state"]["snapshot_digest"]
                if args.events:
                    result["events"] = store.events(args.run_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Saved state can be reconciled with resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
