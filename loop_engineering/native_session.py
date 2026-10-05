"""Read-only compatibility inspection and explicit, expiring host activity reports."""

from datetime import datetime
import hashlib

from . import __version__
from .contracts import ROOT, ContractError
from .host_install import PREVIOUS_SKILLS, SKILL
from .native_workers import text

PROTOCOL = "2.0"
FEATURES = ["bounded_rework", "verification_observations", "dependency_observation_reuse",
            "parallel_native_drafts", "effective_verification_budget", "host_heartbeat"]


def fingerprint():
    digest = hashlib.sha256()
    for folder, suffix in (("loop_engineering", ".py"), ("schemas", ".json")):
        for path in sorted((ROOT / folder).glob("*" + suffix)):
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def compatibility(service):
    from .workspace import safe_path
    published = hashlib.sha256((SKILL / "SKILL.md").read_bytes()).hexdigest()
    rows = []
    for name in (".agents/skills/loop-engineering/SKILL.md", ".claude/skills/loop-engineering/SKILL.md",
                 ".loop/native-host-instructions.md"):
        try:
            path = safe_path(service.project, name)
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_size > 1048576:
                raise ContractError("Instructions are not a bounded regular file")
            installed = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append({"path": name, "sha256": installed, "status": "current" if installed == published else
                         "upgrade_available" if installed in PREVIOUS_SKILLS else "customized"})
        except (OSError, ValueError) as exc:
            rows.append({"path": name, "status": "unavailable", "problem": str(exc)[:256]})
    disk = fingerprint()
    return {"framework_version": __version__, "protocol_version": PROTOCOL, "features": FEATURES,
            "loaded_fingerprint": service.loaded_fingerprint, "disk_fingerprint": disk,
            "restart_required": service.loaded_fingerprint != disk, "published_skill_sha256": published,
            "installed_instructions": rows,
            "migration": "Reconnect after a framework change. Upgrade only exact published instructions with host-install; preserve customized instructions and frozen batches. New opt-in policies apply to new plans; no in-place contract migration."}


def heartbeat(service, team_id, owner, activity, task_id=None, ttl_seconds=120):
    text(owner, "host owner", 256)
    text(activity, "host activity", 2048)
    if type(ttl_seconds) is not int or not 15 <= ttl_seconds <= 300:
        raise ContractError("Host heartbeat lifetime must be between 15 and 300 seconds")
    service._data(team_id, native=True)
    with service.team.teams.writer(team_id) as data:
        if data["status"] != "ACTIVE" or service.team.teams.signal(team_id):
            raise ContractError("Stopped team cannot report active host work")
        if task_id is not None and task_id not in data["tasks"]:
            raise ContractError("Host activity needs an existing task")
        now = int(service.clock())
        previous = data["native_host"].get("heartbeat")
        if previous and previous["owner"] != owner and now < previous["expires_epoch"]:
            raise ContractError("Another host recently reported activity; inspect its work before takeover")
        data["native_host"]["heartbeat"] = dict(owner=owner, activity=activity, task_id=task_id,
            observed_epoch=now, expires_epoch=now + ttl_seconds)
        service.team.teams.save(data, "host.heartbeat")
    return activity_status(service, data)


def activity_status(service, data):
    row = data.get("native_host", {}).get("heartbeat")
    now = service.clock()
    state = "unknown" if row is None else "recent_report" if now < row["expires_epoch"] else "stale_report"
    if data["status"] != "ACTIVE" or service.team.teams.signal(data["team_id"]):
        state = "stopped"
    with service.team.store.connect() as connection:
        first = connection.execute("SELECT envelope FROM team_events WHERE id=? ORDER BY revision LIMIT 1",
                                   (data["team_id"],)).fetchone()
    initial = service.team.teams._verify(first[0], data["team_id"])["data"]
    started = datetime.fromisoformat(initial["updated_at"].replace("Z", "+00:00")).timestamp()
    ids = {r.get(k) for r in data["records"].values() for k in ("child_run_id", "integration_run_id")} - {None}
    recorded = sum(service.team.store.get(identity).get("elapsed_ms", 0) for identity in ids if service.team._exists(identity))
    return {"state": state, "report": row, "team_elapsed_seconds": max(0, now - started),
            "recorded_controller_seconds": recorded / 1000, "host_usage": "unknown",
            "assurance": "A recent host report is not process attestation or a lease. Polling never renews activity, deadlines or budgets. Staleness does not authorize duplicate work or replay."}
