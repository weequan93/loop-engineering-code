"""Migrate a Loop Engineering v1 project (team, tasks, answers, drafts) into a v2 goal.

Steps (all reported, nothing silent):
1. Export v1 through the v1 code (``legacy/v1/scripts/export_v1.py``, signature-checked reads).
2. Refuse while a v1 supervisor process is still alive.
3. Apply the current unsubmitted draft of the running task to the project, but only for
   files that still match the draft's base. Conflicts stay in the migration bundle and
   become the first task.
4. Move the v1 ``.loop`` files into ``.loop/legacy-v1/`` and remove the v1 host entries.
5. Create a draft v2 goal: v1 tasks with their status (COMPLETE → done), command
   checks → checks, final-task commands → acceptance, review/interaction checks →
   reviews, answered questions → decisions, and the last supervisor report → handoff.
6. Install the v2 hosts. The goal stays a draft until a human runs ``loop approve``.

The v1 state directory is never modified or deleted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import engine, model
from .install import LEGACY_SKILLS, install, uninstall_legacy
from .store import LOOP_DIR, Project
from .util import LoopError, atomic_write, file_digest, now, read_json, slugify

LEGACY_ROOT = Path(__file__).resolve().parent.parent / "legacy" / "v1"


def safe_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", str(value).lower()).strip("-.")[:64]
    return value or "item"


def convert_check(check: dict) -> tuple[dict | None, dict | None]:
    """Return (command check, review gate) for one v1 check."""
    cid = safe_id(check.get("id", "check"))
    if check.get("type") == "command" and check.get("argv"):
        result = {"id": cid, "argv": [str(a) for a in check["argv"]]}
        if check.get("timeout_seconds"):
            result["timeout"] = min(86400, max(1, int(check["timeout_seconds"])))
        if check.get("cwd") and not str(check["cwd"]).startswith("/"):
            result["cwd"] = check["cwd"]
        if check.get("description"):
            result["description"] = str(check["description"])[:2000]
        return result, None
    procedure = check.get("procedure") or check.get("description") or "Follow the original v1 check."
    if isinstance(procedure, (list, dict)):
        procedure = json.dumps(procedure, ensure_ascii=False)
    by = "human" if check.get("type") == "interaction" else "agent"
    return None, {"id": cid, "by": by, "instructions": (str(check.get("description") or "") + "\n" +
                                                         str(procedure))[:8000].strip()}


def run_export(project: Project, state_dir: Path, out: Path) -> dict:
    script = LEGACY_ROOT / "scripts" / "export_v1.py"
    if not script.is_file():
        raise LoopError(f"Legacy exporter missing at {script}")
    venv = LEGACY_ROOT.parent.parent / ".venv" / "bin" / "python"
    python = os.environ.get("LOOP_LEGACY_PYTHON") or (str(venv) if venv.exists() else sys.executable)
    result = subprocess.run([python, str(script), "--project", str(project.root), "--state-dir", str(state_dir),
                             "--out", str(out)], capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise LoopError("v1 export failed:\n" + (result.stderr or result.stdout)[-4000:])
    return read_json(out / "export.json")


def plan_drafts(project: Project, export: dict, bundle: Path) -> list[dict]:
    plans = []
    for draft in export.get("drafts", []):
        entry = {"id": draft["id"], "task": draft["task_id"], "error": draft.get("error"), "apply": [],
                 "conflicts": [], "skipped": []}
        for change in draft.get("changes", []):
            target = project.root / change["path"]
            if change.get("kind") != "file" and change["operation"] != "delete":
                entry["skipped"].append({"path": change["path"], "reason": f"{change.get('kind')} entries are not migrated"})
                continue
            current = file_digest(target) if target.is_file() else None
            expected = change["base_sha256"]
            if current == change["new_sha256"]:
                entry["skipped"].append({"path": change["path"], "reason": "already applied"})
            elif current == expected:
                entry["apply"].append(change)
            else:
                entry["conflicts"].append({"path": change["path"], "expected": expected, "current": current})
        entry["files"] = str(bundle / "drafts" / draft["id"])
        plans.append(entry)
    return plans


def apply_draft(project: Project, plan: dict) -> int:
    count = 0
    source = Path(plan["files"])
    for change in plan["apply"]:
        target = project.root / change["path"]
        if change["operation"] == "delete":
            if target.is_file():
                target.unlink()
                count += 1
            continue
        data = (source / change["path"]).read_bytes()
        if "sha256:" + __import__("hashlib").sha256(data).hexdigest() != change["new_sha256"]:
            raise LoopError(f"Exported draft file {change['path']} does not match its recorded hash")
        mode = (source / change["path"]).stat().st_mode & 0o777
        atomic_write(target, data, mode or 0o644)
        count += 1
    return count


def build_goal(project: Project, export: dict, draft_plans: list[dict]) -> tuple[dict, list[dict], list[str]]:
    workflow = export.get("workflow") or {}
    team = export.get("selected_team") or {}
    contracts = export.get("task_contracts", {})
    final = team.get("final_task") or workflow.get("final_task")
    acceptance, reviews, constraints = [], [], []
    final_contract = contracts.get(final) or {}
    for check in final_contract.get("checks") or []:
        command, gate = convert_check(check)
        if command:
            acceptance.append(command)
        elif gate:
            reviews.append(gate)
    if not acceptance:
        seen = set()
        for contract in contracts.values():
            for check in contract.get("checks") or []:
                command, _ = convert_check(check)
                if command and command["id"] not in seen:
                    seen.add(command["id"])
                    acceptance.append(command)
    scope = team.get("scope") or workflow.get("scope") or ""
    if isinstance(scope, (dict, list)):
        scope = json.dumps(scope, ensure_ascii=False)
    objective = (f"Continue the {project.root.name} work migrated from Loop Engineering v1 "
                 f"(team {team.get('team_id')}, workflow {workflow.get('workflow_id')}).\n\n"
                 f"Original scope:\n{scope}\n\nThe original task contracts (objective, criteria, checks) are in "
                 ".loop/legacy-v1/tasks/. Tasks completed in v1 stay done; the final acceptance and reviews "
                 "re-verify the integrated result.")
    context = [p for p in ("spec.md", "AGENTS.md", "README.md", ".loop/legacy-v1/spec.md", ".loop/legacy-v1/plan.md",
                           ".loop/legacy-v1/workflow-tasks.json", ".loop/legacy-v1/tasks/")
               if (project.root / p).exists() or p.startswith(".loop/legacy-v1/")]
    constraints += ["Do not redo tasks completed in v1 unless a check proves them broken.",
                    "Follow each original task contract in .loop/legacy-v1/tasks/<task>.json (criteria and limits).",
                    "Preserve answered decisions and existing evidence; never weaken original checks."]
    title = f"{project.root.name}: {workflow.get('workflow_id') or 'migrated v1 work'}"
    goal = {"id": safe_id(f"{project.root.name}-{workflow.get('workflow_id') or 'v1'}")[:64], "kind": "develop",
            "title": title[:200], "objective": objective[:20000], "context": context, "constraints": constraints,
            "acceptance": acceptance, "reviews": reviews,
            "policy": {"max_iterations": 80, "max_hours": 48.0, "turn_timeout_minutes": 120,
                       "check_timeout_seconds": 3600},
            "agent": {"adapter": "codex"}}

    records = team.get("records", {})
    tasks = []
    order = [t["id"] for t in workflow.get("tasks", [])] or list(team.get("tasks", {}))
    known = {safe_id(t) for t in order}
    for task_id in order:
        contract = contracts.get(task_id) or {}
        info = (team.get("tasks") or {}).get(task_id) or next(
            (t for t in workflow.get("tasks", []) if t["id"] == task_id), {})
        checks = []
        for check in contract.get("checks") or []:
            command, gate = convert_check(check)
            if command:
                checks.append(command)
        criteria = contract.get("criteria") or []
        detail = (str(contract.get("objective") or "") + "\n\nCriteria:\n" +
                  "\n".join(f"- {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}" for c in criteria)
                  + f"\n\nOriginal contract: .loop/legacy-v1/tasks/{task_id}.json")[:8000]
        record = records.get(task_id, {})
        status = {"COMPLETE": "done", "RUNNING": "active"}.get(record.get("status"), "pending")
        tasks.append({"values": {"id": safe_id(task_id), "title": f"{info.get('phase') or 'task'}: {task_id}"[:200],
                                 "detail": detail, "role": info.get("role"),
                                 "depends_on": [safe_id(d) for d in info.get("depends_on", []) if safe_id(d) in known],
                                 "checks": checks},
                      "status": status, "legacy_status": record.get("status"), "reason": record.get("reason")})
    integrate = [p for p in draft_plans if p["conflicts"] or p.get("error")]
    if integrate:
        detail = ("Unsubmitted v1 draft work could not be applied automatically.\n" + "\n".join(
            f"- {p['id']} (task {p['task']}): {len(p['conflicts'])} conflicting files, files at {p['files']}"
            + (f"; error: {p['error']}" if p.get('error') else "") for p in integrate) +
            "\nCompare each conflicting file with the bundle copy and merge what is still needed.")
        tasks.insert(0, {"values": {"id": "integrate-v1-drafts", "title": "Integrate unsubmitted v1 draft work",
                                    "detail": detail[:8000], "depends_on": [], "checks": []},
                         "status": "pending", "legacy_status": None, "reason": None})
        for task in tasks[1:]:
            if task["status"] != "done":
                task["values"]["depends_on"] = sorted(set(task["values"]["depends_on"]) | {"integrate-v1-drafts"})
    return goal, tasks, [p["id"] for p in integrate]


def migrate(project: Project, state_dir: Path, *, dry_run: bool = False, hosts: list[str] | None = None,
            approve: bool = False) -> dict:
    state_dir = Path(state_dir).expanduser().resolve()
    if not (state_dir / "state.sqlite3").is_file():
        raise LoopError(f"No v1 state database at {state_dir}/state.sqlite3")
    if (project.loop / "goals").is_dir() and any((project.loop / "goals").iterdir()):
        raise LoopError("This project already has v2 goals; migration runs once")
    stamp = now().replace(":", "").replace("-", "")[:15]
    bundle = Path(os.environ.get("TMPDIR", "/tmp")) / f"loop-v1-export-{project.root.name}-{stamp}"
    export = run_export(project, state_dir, bundle)
    from . import procs
    live = [s for s in export.get("supervisors", []) if s.get("status") == "RUNNING" and
            (s.get("pending") or {}).get("pid") and procs.alive(s["pending"]["pid"])]
    if live:
        raise LoopError("v1 supervisors are still running: " + ", ".join(s["id"] for s in live) +
                        ". Pause them first (legacy/v1/scripts/loop.py host-supervisor-control --action pause).")
    draft_plans = plan_drafts(project, export, bundle)
    goal_values, tasks, integrate = build_goal(project, export, draft_plans)
    summary = {
        "project": str(project.root), "v1_team": (export.get("selected_team") or {}).get("team_id"),
        "tasks": [{"id": t["values"]["id"], "status": t["status"], "v1": t["legacy_status"]} for t in tasks],
        "acceptance": [c["id"] for c in goal_values["acceptance"]], "reviews": [r["id"] for r in goal_values["reviews"]],
        "drafts": [{"id": p["id"], "task": p["task"], "apply": len(p["apply"]), "conflicts": len(p["conflicts"]),
                    "skipped": len(p["skipped"]), "error": p.get("error")} for p in draft_plans],
        "export_bundle": str(bundle), "dry_run": dry_run}
    if dry_run:
        return summary

    # Apply clean drafts first (before anything moves), then relocate v1 files.
    applied = {p["id"]: apply_draft(project, p) for p in draft_plans if not p["conflicts"] and not p.get("error")}
    legacy = project.loop / "legacy-v1"
    legacy.mkdir(parents=True, exist_ok=True)
    for entry in list(project.loop.iterdir()) if project.loop.is_dir() else []:
        if entry.name != "legacy-v1":  # no v2 goals exist yet, so everything here is v1
            shutil.move(str(entry), str(legacy / entry.name))
    for skill in LEGACY_SKILLS:
        path = project.root / skill
        if path.exists():
            (legacy / "skills").mkdir(exist_ok=True)
            shutil.move(str(path), str(legacy / "skills" / Path(skill).name))
    shutil.copytree(bundle, legacy / "export", dirs_exist_ok=True)
    removed = uninstall_legacy(project)

    store = project.create_goal(goal_values)

    def populate(goal, state):
        for item in tasks:
            task = model.task(item["values"])
            task["status"] = item["status"]
            if item["status"] == "done":
                task["summary"] = f"Completed in Loop v1 (team {summary['v1_team']})."
                task["finished_at"] = now()
            if item.get("reason"):
                task["notes"].append({"at": now(), "kind": "handoff", "by": "migration",
                                      "text": f"v1 status {item['legacy_status']}: {str(item['reason'])[:3000]}"})
            state["tasks"].append(task)
        engine._assert_acyclic(state["tasks"])
        for question in export.get("questions", []):
            answers = question.get("answers") or []
            state["questions"].append({
                "id": "q-" + safe_id(question.get("id", "v1"))[:40], "kind": "question",
                "text": str(question.get("question", ""))[:4000], "options": [], "blocking": False,
                "asked_at": now(), "by": "v1", "action": None,
                "answer": "; ".join(str(a) for a in answers)[:8000] if answers else None,
                "answered_at": now() if answers else None, "answered_by": "v1 user" if answers else None})
        reports = [s for s in export.get("supervisors", []) if s.get("last_report")]
        handoff = [f"Migrated from Loop v1 on {now()}. v1 team {summary['v1_team']}."]
        for plan in draft_plans:
            handoff.append(f"v1 draft {plan['id']} (task {plan['task']}): applied {applied.get(plan['id'], 0)} files, "
                           f"{len(plan['conflicts'])} conflicts, bundle {legacy / 'export' / 'drafts' / plan['id']}.")
        for report in reports[:2]:
            last = report["last_report"]
            handoff.append(f"Last v1 supervisor report ({report['id']}, {last.get('status')}): "
                           f"{str(last.get('summary'))[:2500]}")
        handoff.append("Re-check any v1 'permission' blocker before trusting it: v1 used /bin/ps (setuid, refused "
                       "in sandboxes) and treated macOS killpg EPERM on zombie-only groups as a permission error.")
        state["notes"].append({"at": now(), "kind": "handoff", "by": "migration", "text": "\n".join(handoff)[:8000]})
        state["migration"] = {"from": "loop-engineering v1", "at": now(), "team": summary["v1_team"],
                              "bundle": str(legacy / "export"), "applied": applied, "integrate": integrate}
        state["status_reason"] = "Migrated from v1. Review goal.json (acceptance, reviews, policy), then `loop approve`."
    store.mutate("goal.migrated", populate, team=summary["v1_team"])
    result = install(project, hosts or ["claude", "codex", "generic"])
    if approve:
        store.mutate("goal.approved", lambda g, s: engine.approve(g, s, "migration"))
    report = legacy / "MIGRATION.md"
    atomic_write(report, "# Loop v1 → v2 migration\n\n```json\n" + json.dumps(
        {**summary, "applied": applied, "removed_host_entries": removed, "installed": result["changed"],
         "goal": store.id}, indent=2, ensure_ascii=False) + "\n```\n\nThe v1 state directory "
        f"`{state_dir}` was not modified. Delete it only after the migrated goal is accepted.\n")
    return {**summary, "goal": store.id, "applied": applied, "removed_host_entries": removed,
            "report": str(report), "next": "Review .loop/goals/<id>/goal.json, then `loop approve` and `loop start`."}


__all__ = ["migrate", "LOOP_DIR", "slugify"]
