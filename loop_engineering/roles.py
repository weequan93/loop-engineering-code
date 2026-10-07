"""Specialist roles: each development task runs with its role's expertise.

A task's ``role`` (architect, backend, frontend, mobile, data, tester, security, devops,
or any project-defined name) selects:

- a role guide: ``.loop/roles/<role>.md`` in the project if present, else the built-in guide;
- optionally a host skill or extra guide via ``agent.roles.<role>.skill`` (a skill name the
  host has installed, or a repository path whose content is included);
- optionally its own adapter, model and reasoning effort (see ``engine.profile_for``).
"""

from __future__ import annotations

from pathlib import Path

BUILTIN = Path(__file__).parent / "roles"
LIMIT = 12000

# Workflow → stage → role. A task's own role (develop/repair) or a review's role wins.
# Override per project in .loop/workflows/<workflow>.json: {"roles": {"requirements": "my-analyst"}}.
WORKFLOWS = {
    "develop": {"label": "Development", "stages": [
        ("intake", "product"), ("requirements", "analyst"), ("requirements_review", "product"),
        ("solution", "architect"), ("test_design", "test-designer"), ("plan", "tech-lead"),
        ("resources", "resource-planner"), ("plan_review", "plan-reviewer"), ("develop", None),
        ("repair", "tester"), ("review", "reviewer"), ("final_acceptance", "acceptance-lead"),
        ("product_acceptance", "product")]},
    "operate": {"label": "Operations", "stages": [
        ("remediate", "sre"), ("develop", None), ("repair", "sre"), ("review", "reviewer")]},
    "investigate": {"label": "Investigation", "stages": [
        ("plan", "investigator"), ("develop", "investigator"), ("report", "investigator"),
        ("repair", "investigator"), ("review", "reviewer")]},
}


def workflow(project_root: Path, kind: str) -> dict:
    import json
    base = WORKFLOWS.get(kind, WORKFLOWS["develop"])
    roles = {stage: role for stage, role in base["stages"]}
    custom = project_root / ".loop" / "workflows" / f"{kind}.json"
    if custom.is_file():
        try:
            data = json.loads(custom.read_text(encoding="utf-8"))
            roles.update({k: v for k, v in (data.get("roles") or {}).items() if isinstance(v, str) or v is None})
        except (OSError, ValueError):
            pass
    return {"kind": kind, "label": base["label"], "roles": roles, "order": [s for s, _ in base["stages"]]}


def stage_role(project_root: Path | None, goal: dict, stage: str | None) -> str | None:
    if not stage or project_root is None:
        return dict(WORKFLOWS.get(goal["kind"], WORKFLOWS["develop"])["stages"]).get(stage) if stage else None
    return workflow(project_root, goal["kind"])["roles"].get(stage)


def available(project_root: Path) -> dict[str, str]:
    """Role name → where its guide comes from."""
    roles = {p.stem: "built-in" for p in BUILTIN.glob("*.md")}
    custom = project_root / ".loop" / "roles"
    if custom.is_dir():
        roles.update({p.stem: str(p.relative_to(project_root)) for p in custom.glob("*.md")})
    return dict(sorted(roles.items()))


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= LIMIT else text[:LIMIT] + "\n… (truncated)"


def guide(project_root: Path, role: str | None, profile: dict | None = None) -> str:
    if not role:
        return ""
    parts = []
    custom = project_root / ".loop" / "roles" / f"{role}.md"
    builtin = BUILTIN / f"{role}.md"
    if custom.is_file():
        parts.append(_read(custom))
    elif builtin.is_file():
        parts.append(_read(builtin))
    else:
        parts.append(f"# Role: {role}\n\nWork as an expert {role}. Follow the project's conventions for this kind "
                     "of work, and prove the result with real checks.")
    skill = (profile or {}).get("skill")
    if skill:
        candidate = (project_root / skill).resolve()
        if project_root.resolve() in candidate.parents and candidate.is_file():
            parts.append(f"## Skill: {skill}\n\n{_read(candidate)}")
        else:
            parts.append(f"## Skill\n\nUse the `{skill}` skill from your host for this task, and follow its "
                         "instructions.")
    return "\n\n".join(parts)
