# Native coordination through completion

The Codex/Claude host owns the conversation and native agents. Loop supplies
durable task admission, scoped integration, real checks, next actions and evidence
gates. The host should keep working through the agreed batch while those next
actions are executable. This does not provide an unattended host scheduler or
guarantee that a model will continue after its turn ends.

## Raise material questions during setup

The SPEC/INTAKE packet includes execution-readiness guidance. Read existing docs
and actual answers before asking about scope, acceptance devices/permissions,
local delivery versus release, data/services or spending. Ask only unresolved
decisions that affect this batch, grouped together. Routine implementation choices
and already authorized work do not require repeated approval. Unexpected material
dependencies can still require a later question.

`loop_plan_preflight(team_id, contracts)` inspects proposed `{task, engine}` objects
before they are frozen. It runs no checks/models, creates no evaluator keys and
writes no project controls. It distinguishes:

- `user_input`: a declared decision lacks an actual recorded answer;
- `engineering`: a producer/executable, coverage declaration or rerun reserve
  needs work;
- `external_dependency`: required independent evidence has no available route.

The host turns engineering gaps into explicit work and raises the necessary human
decisions early. Independent authorized work may continue while an acceptance
dependency is unresolved. Plan submission may add required role reviews; inspect
the submitted plan's `verification_preflight` as well. A signing key alone does
not establish a connected evaluator.

Tasks may declare `extensions.verification_plan`:

```json
{
  "measurements": [{
    "check_id": "performance",
    "required": ["component_cpu", "product_cpu", "million_file_scan"],
    "produced": ["component_cpu", "product_cpu", "million_file_scan"],
    "producer_paths": ["scripts/measure_product.py"]
  }],
  "decision_ids": ["acceptance-environment"],
  "repair_reserve_seconds": 600
}
```

Validation rejects unknown/duplicate check IDs, unsafe producer paths, declared
measurement gaps and command timeouts plus repair reserve exceeding the existing
task wall limit. Intake refuses referenced decisions without actual answers.
Preflight reports producer paths that are not yet files and a reserve smaller
than one full command-check allowance. The reserve is a planning declaration;
it does not increase or reset the controller's cumulative limits. External check
durations and real measurement coverage still require inspection. A declared
producer can be incorrect; only real evidence can satisfy acceptance. Legacy
tasks remain valid, with undeclared coverage visible as a planning issue.

## Keep long checks responsive

MCP `loop_task_submit`, `loop_workbench_submit` and `loop_evaluation_run` return a
durable operation ID while the original controller performs the work in a
background thread. Workbench bytes are frozen before admission. Changes made to
the draft after admission cannot enter that operation. Direct Python service
methods retain their synchronous behavior.

Use `loop_operation_status` with an optional `wait_seconds` from 0 to 5, then
`loop_next`. Progress and pause/cancel calls remain available during long checks.
The dashboard shows operation state and elapsed time separately from task
completion. `FINISHED` means the call returned; it does **not** mean the checks
passed. Failed checks return to scoped repair when the original controller
allows another iteration. A consumed writer request's expired deadline cannot
block that next iteration. Repeated failures and exhausted budgets remain gates.

An OS lock serializes background operations per team across connections. Duplicate
task/workbench submissions return the original operation ID. One client cannot
take over another live runner. The authenticated team journal retains payload
references, errors and history, bounded to 128 operations; the compact view shows
the most recent 20, and older IDs remain queryable. Usage for host conversation
and native agents remains unknown.

After a server crash, `loop_next` returns `loop_operation_recover`. Recovery first
acquires the original operation lock, then uses existing child reconciliation for
owned processes and edits. It retains the child, cumulative limits, proposal
history and acceptance checks. It never blindly resubmits an uncertain proposal.
If the child already succeeded, it collects its actual result. Ambiguous effects,
changed frozen inputs and unavailable capabilities remain explicit blockers.
User pause/cancel is preserved. A normal MCP service shutdown asks its controllers
to pause and clean up; the service must remain running for background execution.

Scope expansion, resetting terminal budgets and reopening completed tasks are
not authorized by this continuation mechanism. A failed review requiring changes
outside its frozen repair scope needs a reviewed recovery batch; requirements and
acceptance criteria must not be weakened to make it finish.

## Update an existing project

After the current batch completes, update the project's shipped host instructions
using the same external state directory, then reconnect its MCP service before
starting the next accepted batch:

```bash
cd /Users/super/Documents/ai/loop-engineering-code
.venv/bin/python scripts/loop.py host-install /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare --host codex
```

For other hosts, use `--host claude-code` or `--host claude-desktop`. Exact previous
shipped skills can be upgraded. Customized skills/configurations are preserved
and reported for review. This does not reinitialize the project or migrate a
running team's frozen contracts. Changed instruction files can invalidate a
running request or candidate; upgrading an unfinished batch requires its reviewed
recovery path. Resume the existing conversation and ask Loop to inspect the
current team and follow its next action within the agreed scope. Explicitly
authorize resume if it was paused, and authorize a new milestone when the prior
batch has completed.

Offline tests cover early decision/coverage validation, live local subprocess
cancellation, cross-connection ownership, stable draft capture, duplicate calls,
crash reconciliation and continued repair with the same child. No live model
evaluation is implied; follow [implementation readiness](implementation-status.md)
before any future live evaluation.
