---
name: loop
description: Run development, operations or investigation goals through the Loop Engineering controller, with verified completion, autonomous turns and visible status. Use when the user asks to build or ship a feature or app end to end, keep a service healthy, troubleshoot a problem until the root cause is found, or check, continue, pause or answer a running loop.
---

# Loop Engineering

This project is connected to a Loop controller through the `loop` MCP server. The
controller owns state, checks and completion. You do the work. The status is always in
`.loop/STATUS.md`, from `loop status`, or on the `loop dashboard` page.

The CLI in this project is `.loop/bin/loop`; `loop` also works when it is on PATH. Every MCP
tool has a CLI equivalent, so use the CLI if the MCP tools are not loaded.

## 1. Intake (new goal)

1. Read the project docs the user points to. Ask the user only about what changes scope,
   permissions, money, environments or acceptance. Ask everything together, once.
2. Draft the goal with `loop_goal_draft`:
   - `kind`: `develop` (build or change software), `operate` (keep something healthy on a
     schedule) or `investigate` (find the root cause).
   - `objective`: the outcome in the user's words, plus the success criteria.
   - `acceptance`: real commands the controller can run. Examples: tests, build, lint, an
     end-to-end script, a health probe. `develop` and `operate` need at least one.
   - `constraints`, `context` (doc paths), `policy` (limits, `approval_required`),
     `agent.adapter` (`claude` | `codex` | a configured command) and, for `operate`,
     `schedule.interval_minutes`.
3. Show the user the goal, especially the acceptance checks and limits. Call
   `loop_goal_approve(confirmed_by_user=true)` only after they confirm.
4. Ask whether to run it **here in this chat** (step by step) or **autonomously**. For
   autonomous runs, the user runs `loop start`, or you run it after they agree. It keeps
   working after this chat ends.

New develop goals use the **full pipeline** by default: requirements → plan → plan review →
develop → staged tests → reviews → final acceptance. Give acceptance checks a `stage`
(unit, integration, e2e, performance, security, regression, acceptance). To give stages or
roles their own model, use `agent.stages` (requirements, plan, plan_review, develop, repair,
review, final_acceptance, planner) and `agent.roles` (architect, backend, tester, ...). Each
one takes `{adapter, model, effort}`.

## 2. Working a turn (in chat or autonomous)

- Start with `loop_next` and do exactly that assignment.
- Requirements stage: record every requirement from the documents with `loop_requirements`
  (id, text, source, verify), then call `final=true`. Don't write code in this stage.
- Plan stage: every task lists the requirements it `covers` and has real checks. Call
  `loop_plan(final=true)` to submit the plan for independent review.
- Plan: use `loop_plan` to create tasks, each with real, fast `checks`.
- Work: implement, run the tests yourself, then call `loop_task(action="done", summary=...)`.
  The controller runs that task's checks. If they fail, fix the cause and claim done
  again.
- When every task is finished, call `loop_finish(report=...)`. The controller runs the
  acceptance checks. It reports done, or it hands you a repair assignment.
- Before you stop, record `loop_note(kind="handoff")`: what changed, what is next, and any
  traps.

## 3. Humans, approvals, blockers

- Use `loop_ask(kind="approval", action=...)` before any action in `approval_required`
  (deploys, data deletion, spending, messages, credentials, history rewrites). Wait for the
  answer.
- In chat, relay the user's answers with `loop_answer`. Record only what they actually said.
- Use `loop_block` only when no progress is possible. permission, environment and external
  blockers need the exact reproduction and what you already tried. "Permission denied" is
  often a fixable tooling problem, so investigate it first.

## 4. Status questions

When the user asks for progress, call `loop_status` (or read `.loop/STATUS.md`). Report the
status, next action, tasks done/total, acceptance results, open questions and budget used.
Don't guess. Show the user the CLI commands they can run:
`loop status --watch`, `loop dashboard`, `loop answer <id> "..."`, `loop pause`, `loop resume`,
`loop stop`.

## Rules

- Never edit `.loop/` files by hand. Never weaken, skip or delete checks to get green.
- A model's claim is not evidence; only controller-run checks are.
- Stay inside the goal's constraints. Scope changes go through `loop_goal_draft` and need
  the user's re-approval.
