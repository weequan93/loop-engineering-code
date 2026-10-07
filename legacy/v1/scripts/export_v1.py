"""Export a Loop Engineering v1 project for migration to v2 (read-only on v1 state).

Uses the v1 code so team checkpoints are read through their signature checks.
Writes one JSON document and copies the files of unsubmitted current drafts.

    python legacy/v1/scripts/export_v1.py --project P --state-dir S --out DIR
"""

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop_engineering.store import Store  # noqa: E402
from loop_engineering.team_engine import TeamController  # noqa: E402

TASK_FIELDS = ("objective", "kind", "criteria", "checks", "scope", "limits", "authorization")


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    project = args.project.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    store = Store(args.state_dir)
    team = TeamController(store)
    runs = [r for r in team.teams.runs() if Path(r["workspace"]).resolve() == project]
    unfinished = [r for r in runs if r["status"] not in {"COMPLETE", "CANCELLED"}]
    selected = (unfinished or runs or [None])[0]
    result = {"project": str(project), "state_dir": str(args.state_dir.resolve()),
              "teams": runs, "selected_team": None, "supervisors": [], "drafts": [],
              "workflow": load(project / ".loop/workflow-tasks.json"),
              "questions": (load(project / ".loop/questions.json") or {}).get("questions", []),
              "task_contracts": {}}
    for path in sorted((project / ".loop/tasks").glob("*.json")):
        if not path.name.endswith("-engine.json"):
            contract = load(path) or {}
            result["task_contracts"][path.stem] = {k: contract.get(k) for k in TASK_FIELDS}
    try:
        from loop_engineering.native_supervisor_store import SupervisorStore
        for row in SupervisorStore(store).rows(project):
            result["supervisors"].append({k: row.get(k) for k in (
                "id", "status", "reason", "turns", "known_tokens", "dispatch_ms", "last_report", "team_id",
                "pending", "session_id")})
    except Exception as exc:  # older states have no supervisor table
        result["supervisor_error"] = str(exc)
    if selected:
        data = team.teams.get(selected["team_id"])
        host = data.get("native_host", {})
        result["selected_team"] = {
            "team_id": data["team_id"], "status": data["status"], "stage": data.get("stage"),
            "reason": data.get("reason"), "revision": data.get("revision"), "updated_at": data.get("updated_at"),
            "scope": (data.get("plan") or {}).get("scope"), "final_task": (data.get("plan") or {}).get("final_task"),
            "records": {k: {"status": v["status"], "reason": v.get("reason"), "child_run_id": v.get("child_run_id"),
                            "request_id": (v.get("request") or {}).get("id")} for k, v in data["records"].items()},
            "tasks": {k: {x: v.get(x) for x in ("id", "phase", "role", "depends_on", "outputs")}
                      for k, v in data["tasks"].items()},
            "usage": host.get("usage"),
        }
        profile = team.profile(data)
        for wb_id, wb in host.get("workbenches", {}).items():
            record = data["records"].get(wb["task_id"], {})
            current = (record.get("request") or {}).get("id") == wb.get("request_id")
            if wb.get("status") != "DRAFT" or not current or record.get("status") != "RUNNING":
                continue
            result["drafts"].append(export_draft(team, profile, wb_id, wb, out))
    (out / "export.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
    print(str(out / "export.json"))


def export_draft(team, profile, wb_id, wb, out):
    entry = {"id": wb_id, "task_id": wb["task_id"], "directory": wb.get("directory"), "changes": [], "error": None}
    directory = Path(wb.get("directory") or "")
    if not directory.is_dir():
        entry["error"] = "draft directory is missing"
        return entry
    try:
        base = json.loads(team.snapshots.read(wb["base"]).decode())
        candidate = team.snapshots.capture(directory, profile)
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry
    old = {f["path"]: f for f in base["manifest"]["files"]}
    new = {f["path"]: f for f in candidate["manifest"]["files"]}
    files = out / "drafts" / wb_id
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        change = {"path": name, "base_sha256": before["sha256"] if before else None,
                  "new_sha256": after["sha256"] if after else None,
                  "operation": "delete" if after is None else ("add" if before is None else "modify"),
                  "kind": (after or before)["kind"]}
        if after and after["kind"] == "file":
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory / name, target)
        entry["changes"].append(change)
    return entry


if __name__ == "__main__":
    main()
