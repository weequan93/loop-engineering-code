# Maintaining Loop Engineering 2

- Keep the core small: stdlib-only Python ≥ 3.10, 13 MCP tools, a short skill and short playbooks. Fix a root cause
  instead of adding a new route or tool per incident. Read the lessons table in `docs/design.md` before
  adding surface area.
- Every state change goes through `engine.py` operations inside `GoalStore.mutate`. Never write `state.json`
  any other way, and never add a code path where a model's claim marks something complete without a
  controller-run check or a recorded review.
- Tests must stay offline: use `tests/fixtures/fake_agent.py` (real MCP, scripted behaviour) and local
  commands. Run `python -m unittest discover -s tests -t .` before handing off. Live Claude or Codex turns
  are for explicit, user-requested evaluations only.
- Don't edit the framework checkout while a runner is driving a real project from it. Pause the runner first.
- `legacy/v1/` is the frozen previous version, kept for migration (`legacy/v1/scripts/export_v1.py`)
  and reference. Don't develop it further.
- Document real platform and host limits (sandboxing comes from the host; cost only where reported).
