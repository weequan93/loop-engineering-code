# Loop Engineering 2: design

## Goals

1. An agent can take a goal from intake to verified completion **without a human in every turn**.
2. The same goal works from **Claude Code, Codex, or any CLI agent**, in chat or unattended.
3. Any tool or person can **see the state** at any time, without special access.
4. Completion, budgets and safety gates are **enforced by the controller**, not trusted to the model.

## Architecture

```
            ┌───────────── hosts ──────────────┐
 chat:      │ Claude Code · Codex · Cursor · … │──MCP (13 tools)──┐
 unattended:│ runner → adapter (claude/codex/cmd) per turn         │
            └──────────────────────────────────┘                  ▼
                                                    api.Service (one service layer)
                                                         │
                         engine (state machine) ── checks (controller-run, background jobs)
                                                         │
                         store: .loop/goals/<id>/{goal.json,state.json,events.jsonl,STATUS.md}
```

| Module | Responsibility |
|---|---|
| `model.py` | goal / task / check / review schemas (stdlib validation) |
| `store.py` | files, exclusive locks, hash-chained events, status rendering on every change |
| `engine.py` | `next_action` and every state transition (pure functions over goal + state) |
| `checks.py` | workspace fingerprint, check execution, detached check jobs |
| `procs.py` | process groups via libproc / `/proc` (no `/bin/ps`), zombie-safe cleanup |
| `runner.py` | unattended loop: dispatch, progress measurement, budgets, stop/pause, crash recovery |
| `adapters.py` | `claude`, `codex`, configurable `command` adapters |
| `mcp_server.py` | stdio MCP; interactive / autonomous / reviewer tool sets |
| `status.py`, `dashboard.py` | STATUS.md, status line, local read-only dashboard |
| `install.py`, `migrate.py` | host integration, v1 migration |

### `next_action` order

integrity problem → unapproved or changed contract → terminal or paused → blocking
questions → blocker → (operate: open incident) → active task → ready task → (operate:
health check or idle) → plan → (investigate: report) → acceptance verify / repair → agent
reviews → human reviews → finished.

Actors: `agent` (a turn is useful), `controller` (verify, health check), `human`
(approve, answer, unblock) and `none`. The runner makes model calls **only** for `agent`
actions. While it waits on a human, a schedule or a pause, it only polls files.

### Completion

- `loop_task done`: the controller runs that task's checks in a detached job. Pass → done.
  Fail → attempts + 1; at the limit the goal blocks with the failure output.
- When every task is done, the controller runs acceptance. Fail → repair round (bounded).
  Pass → the reviews:
  - Agent reviews run as a **separate read-only turn**: fresh session, `--sandbox
    read-only` / no edit tools, optionally another adapter or model. A one-time token means
    only that reviewer MCP session can call `loop_review`. A changed workspace invalidates the
    verdict.
  - Human reviews start only after the agent reviews pass. They become approval questions.
- Every result binds to the workspace fingerprint. Any later change means re-verification.

### Safety model (stated honestly)

- The controller enforces the contract (approval digest), task and acceptance checks,
  reviews, the turn, time and cost budgets (cost only where the adapter reports it),
  stagnation, and pause/stop. It also keeps human-only operations away from autonomous
  agents.
- **OS-level containment comes from the host.** Codex runs with `--sandbox workspace-write`
  (`read-only` for reviewers). Claude Code runs with `--permission-mode acceptEdits` and an
  allow-list. Actions in `approval_required` are an instruction plus a question protocol.
  Loop cannot technically stop a shell command the host allows. For hard guarantees, run the
  runner in a container or VM, or narrow the host permissions with `agent.extra_args`.
- State files are visible and agents could edit them. The event hash chain and the
  state digest detect external edits and block until a human runs `loop repair`.

## Lessons carried from v1

| v1 problem (observed 2026-10-05/06) | v2 decision |
|---|---|
| A "permission" blocker stopped two projects for a day. The cause was `/bin/ps` (setuid, refused in seatbelt) plus Darwin EPERM on zombie-only process groups. | `procs.py` uses libproc / `/proc`; EPERM on a quiescent group is "exited". `loop_block` with permission, environment or external requires the exact reproduction and what was tried. The investigate playbook says to challenge such labels. |
| Framework upgrades changed the project candidate (skill file inside the snapshot), so special recovery routes were needed. | Framework paths are excluded from the fingerprint. No snapshot copies of the project. |
| 42 tools, a 31 KB skill, a 5 KB instruction string; a new tool and route per incident. | 13 tools, a short skill, short playbooks. Each turn's prompt is self-contained. |
| State hidden in a private 30 GB SQLite and blob store; progress unclear. | Plain files in `.loop/`, STATUS.md on every change, a dashboard and a status line. |
| One host session accumulated 10–35M tokens. | A fresh session each turn by default, with handoff notes carrying the context. |
| Long checks blocked the MCP request loop. | Checks run in detached jobs; tools wait a bounded time and then return a poll handle. |
| Framework edited while live supervisors ran on it. | Runners are separate processes over stable files; the CLI wrapper pins one checkout. |

## Limits

- One runner per goal. Tasks within a goal run serially; the host may use its own
  sub-agents inside a turn. Parallel goals on one working tree are not coordinated. Use
  separate worktrees.
- Codex loads project `.codex/config.toml` only for trusted projects. The runner passes
  its MCP config with `-c` overrides, so unattended runs do not depend on trust.
- Cost totals cover what the adapters report (Claude reports cost, Codex reports tokens).
