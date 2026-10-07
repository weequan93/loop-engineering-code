"""An ordered queue of goals for one project (``.loop/queue.json``).

When the runner finishes a goal (status ``done``) it moves to the next queued goal that
is not finished. It starts that goal only if a human already approved its contract;
otherwise it stops and notifies. Blocked, limited or failed goals never advance the
queue: a person decides what happens next.
"""

from __future__ import annotations

from . import model
from .util import LoopError, read_json, write_json


def path(project):
    return project.loop / "queue.json"


def load(project) -> list[str]:
    data = read_json(path(project), {"goals": []}) if path(project).is_file() else {"goals": []}
    goals = data.get("goals", []) if isinstance(data, dict) else []
    return [g for g in goals if isinstance(g, str)]


def save(project, goals: list[str]) -> None:
    project.ensure()
    write_json(path(project), {"goals": goals})


def add(project, goal_ids: list[str], position: int | None = None) -> list[str]:
    goals = load(project)
    known = set(project.goal_ids())
    for goal_id in goal_ids:
        if goal_id not in known:
            raise LoopError(f"Unknown goal {goal_id}; create it first")
        if goal_id in goals:
            continue
        if position is None:
            goals.append(goal_id)
        else:
            goals.insert(max(0, position), goal_id)
            position += 1
    save(project, goals)
    return goals


def remove(project, goal_id: str) -> list[str]:
    goals = [g for g in load(project) if g != goal_id]
    save(project, goals)
    return goals


def approved(store) -> bool:
    state = store.state()
    return bool(state["approved_digest"]) and state["approved_digest"] == model.contract_digest(store.goal())


def next_after(project, current_id: str | None) -> str | None:
    """The next unfinished queued goal after ``current_id`` (queue order)."""
    goals = [g for g in load(project) if g in set(project.goal_ids())]
    if current_id in goals:
        goals = goals[goals.index(current_id) + 1:]
    for goal_id in goals:
        if goal_id == current_id:
            continue
        if project.goal(goal_id).state()["status"] not in model.TERMINAL:
            return goal_id
    return None


def view(project) -> list[dict]:
    rows = []
    current = project.current_id()
    for position, goal_id in enumerate(load(project), 1):
        try:
            store = project.goal(goal_id)
            state = store.state()
            rows.append({"position": position, "id": goal_id, "title": store.goal()["title"],
                         "status": state["status"], "approved": approved(store), "current": goal_id == current})
        except LoopError:
            rows.append({"position": position, "id": goal_id, "title": "(missing)", "status": "missing",
                         "approved": False, "current": False})
    return rows
