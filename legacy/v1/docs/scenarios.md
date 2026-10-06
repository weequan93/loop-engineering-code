# Scenario presets

The [native host entry](native-host.zh-CN.md) connects the same development
preset to Codex/Claude conversations through project-scoped MCP tools and a
portable skill. One-time `host-install` replaces repeated user commands with
host-operated requirements/planning/proposal tools and actual progress display.
Native host model calls have unknown usage outside scheduler accounting; hard
caps and required independent evidence remain blockers. Existing automatic
teams are not silently converted.

The development preset also supplies a [requirements setup assignment](requirements-setup.zh-CN.md).
Call `setup PROJECT --state-dir PRIVATE_STATE --adapter codex` to have the
requirements role read docs and collect a spec before development. Missing specs
in `team-run` now enter this stage and stop at the prepared-spec handoff.
Continue the same team ID; its cumulative budget and answers remain intact.

Use a preset when you want the coding host to own task setup and specialist
coordination. The first implemented preset is **development**. The user
provides a spec and answers material questions; the coordinator inspects the
project and prepares task contracts, check commands and assignments.

Setup and context export execute no project commands or model calls. Explicit
`team-run` adds continuous supervised scheduling, shared budgets, bounded repair
and isolated parallel working copies. The portable host route remains available.

## User entrypoint

From the framework checkout, with its Python environment installed:

```bash
.venv/bin/python scripts/loop.py scenarios
.venv/bin/python scripts/loop.py init /absolute/path/to/project \
  --scenario development --spec /absolute/path/to/spec.md
.venv/bin/python scripts/loop.py team-run /absolute/path/to/project \
  --state-dir /absolute/private-state --adapter codex
```

The project directory must already exist. The spec is a bounded UTF-8 text
file; Markdown and ordinary prose both work. No particular section names or
task JSON are required. If you omit `--spec`, the coordinator asks for the
requirements in the coding session and records them in `.loop/spec.md`.

`team-run` selects an actual installed host and prepares tasks from the spec.
See [team execution](team-execution.md) for clarification, continuation and limits.
For the portable route, open the target project in your coding host and send:

> Read `.loop/start.md` and develop the supplied spec. Coordinate the specialist
> team, prepare the tasks and checks, and ask me only material questions.

The coordinator reads the spec before asking questions. It records answers,
chooses technical details supported by the repository, assigns relevant
specialists, integrates their changes, verifies and delivers. The user does
not need to fill in `.loop/task.json` or invent check commands. For an empty
project, the host first prepares a minimal project/test scaffold under the
user's authorized scope; it cannot fabricate existing acceptance tests.

## Generated files

The ordinary `LOOP.md`, project/task templates and handoff remain available.
The preset adds:

| File | Purpose |
| --- | --- |
| `.loop/start.md` | Host entrypoint |
| `.loop/scenario.json` | Checked role roster and dependency-ordered workflow |
| `.loop/team.json` | Spec-bound role decisions, reasons, sources, assignees, coverage and handoffs |
| `.loop/spec.md` | Copied user requirements |
| `.loop/questions.json` | Questions, why they matter, blocking state and ordered answer history |
| `.loop/plan.md` | Host-maintained task ownership, dependencies, acceptance map and shared budget |
| `.loop/workflow.md` | Intake, task preparation, delegation, integration, verification and delivery rules |
| `.loop/workflow-tasks.json` | Coordinator-prepared checked workflow, dependencies, outputs and recipients |
| `.loop/team-policy.json` | Frozen cumulative dispatch, repair, concurrency, deadline and spend limits |
| `.loop/execution.md` | Coordinator guide to continuous and manual execution/recovery |
| `.loop/agents/*.md` | Sixteen professional role contracts, listed below |
| `.loop/agent-protocol.md` | Shared specialist work cycle, evidence/report contents and responsibility coverage |
| `.loop/schemas/*.json` | Task, project, question, team and team-workflow schemas for the coordinator |

Role definitions are reusable. The coordinator activates only the roles the
spec needs; for example, a library task can omit frontend work. The manifest's
workflow guides the host and is not a native `engine.json.stages` graph. Optional
[team execution](team-execution.md) applies phase dependencies, prepares checked
tasks through the selected host, schedules isolated workers and enforces verified
handoffs. Spec interpretation and judgments still belong to actual host requests.

## Professional team and handoffs

| Role ID | Responsibility | Main deliverable |
| --- | --- | --- |
| `coordinator` | Consolidate questions, prepare tasks, assign specialists and integrate | Dependency plan, shared state and delivery |
| `requirements_reviewer` | Review ambiguity, scope, business rules and verifiability before development | Requirements with stable IDs, sources and acceptance scenarios |
| `designer` | Define product flows, UX/UI, interaction states and accessibility | Design specifications and implementation conformance findings |
| `architect` | Define system boundaries, API/data contracts and technical tradeoffs | Architecture/interface decisions and conformance review |
| `frontend` | Implement the agreed user-facing experience | UI code and interaction evidence |
| `backend` | Implement business rules, APIs and persistence | Service/library code and behavioral evidence |
| `tester` | Check functional, integration, negative and regression behavior | Actual test results and reproducible defects |
| `security` | Identify security requirements early and review the integrated candidate | Scoped findings, remediations and verification evidence |
| `performance` | Define workloads/targets early and measure actual performance | Benchmark/load procedure, measurements and threshold result |
| `reviewer` | Independently review implementation quality | Candidate-specific code findings and review result |
| `acceptance` | Independently match the delivered result to the reviewed requirements | Requirement-to-evidence matrix and acceptance recommendation |
| `integrator` | Coordinate interfaces, merge order, conflicts and whole-flow integration | Integrated candidate and integration evidence |
| `devops` | Prepare reproducible builds, CI, environment and delivery procedures | Build artifact, configuration evidence and deployment/rollback procedure |
| `database` | Design data constraints, queries and compatible migrations | Data/migration code, compatibility and recovery evidence |
| `documentation` | Explain delivered behavior, APIs, setup and maintenance | Verified guidance, examples and handover notes |
| `reliability` | Prepare health signals, monitoring and recovery for services | Operational configuration, runbooks and bounded recovery evidence |

The dependency flow is intake → requirements review → team selection → design →
plan → implementation → integration → functional/design/data verification →
security, performance, code and delivery-readiness assessments → requirements
acceptance → delivery.
Security and performance participate during design as well as during final
verification. Acceptance planning begins during requirements review so the
criteria exist before implementation. The coordinator gathers all material
questions into one user-facing conversation.

Requirements review establishes the intended behavior; acceptance evaluates
the delivered behavior. Design defines the user experience; frontend builds
it. Functional test results feed acceptance together with required design,
architecture, security, performance, data, delivery, documentation, reliability
and review evidence as applicable. They must refer to
the integrated candidate, with all blocking findings resolved. A prescribed
human decision requires the actual person; an agent cannot invent sign-off.

The plan template records requirement IDs, sources, quality targets, role
applicability, reviewer identities and evidence. Scale work to the task and
record justified non-applicability before freezing checks. A missing required
capability stays pending; it is not a reason to mark a check inapplicable.
Security/load testing remains bounded to authorized targets and environments.

Role configurations guide the host's professional work. Checked team execution
adds programmatic phase/dependency and handoff gates for a prepared batch; actual
security scans and specialist judgments still use authorized host tools. Frozen
execution plans support supervised browser assertions and bounded HTTP load generation. The accepted task's checks and controller evidence rules determine
controller completion. Continuous scheduling supports parallel isolated working
copies, serial integration and declared dependency preparation/copying; Git worktrees and submodules require another verified integration. See [execution tools](execution-tools.zh-CN.md).

## Selecting a team from the spec

The coordinator interprets the spec and repository, consults specialists and
writes the selection. Setup does not infer a team through keyword matching or
dispatch a model. `team-run` explicitly invokes a coordinator. Every role has
activation rules in the manifest and an
assignment contract defining inputs, writable scope, outputs, handoff and done
conditions. Per-task paths and checks belong in the plan/task contract.

Six responsibilities are required: coordinator, requirements review,
architecture, functional testing, independent code review and acceptance.
Requirements review, architecture and testing can be explicitly covered by the
coordinator on small jobs. Review and acceptance require separate contexts from
implementation/planning; each retains its own responsibility and result.

The other ten roles are selected by scope. UI work calls for design/frontend;
service or library behavior calls for backend. Security and performance are
considered during planning and activated for relevant risks or requirements.
Multi-worker/cross-module changes need integration ownership. Database covers
complex persistence/migration concerns; DevOps covers build/delivery; documentation
covers changed guidance; reliability covers ongoing operation. The manifest
lists allowed covering roles (for example, backend for simple database work).
An active coverer must read and perform the covered role's complete contract.

Current ready selections now supply covered contracts automatically in owner
contexts and recipient inspections. A scoped task receives its role and covering
owner's instructions. Independent reviewers retain their own role context;
unrelated active specialist prompts are not included. Every new role contract
has a concrete professional procedure, described in the [Chinese role guide](development-agents.zh-CN.md).
The declared shared protocol is a frozen input in portable/native team contexts.
Older manifests without that declaration remain supported.

New development profiles default to `120000` context bytes so owner contexts can
retain covered procedures and actual evidence. Existing project limits are
preserved by ordinary initialization. Exceeding an accepted limit fails closed;
changing it requires an explicit edit before the next team starts. Context sizing
does not change team dispatch/spend limits or grant unavailable host capabilities.

The coordinator maintains `.loop/team.json`, with one decision per library role:

| State | Required meaning |
| --- | --- |
| `active` | Actual assigned agent/context in `assignee`, with handoff recipients |
| `covered` | Allowed active role in `covered_by`, with handoff recipients; no separate assignee |
| `inactive` | Concrete non-applicability reason tied to scope; no owner or handoffs |
| `pending` | Missing capability or decision prevents assignment; no owner or handoffs |

Every decision has a reason and `sources` referencing spec sections, answer IDs
or inspected repository paths. `handoff_to` names active or covered roles; for
a covered recipient, use its active owner. No coverage chains, independent
coverers or inactive handoff targets are accepted. Independent roles cannot be
covered or share an assignee label with active implementation/planning roles. Identity labels and
source references are host declarations, not verified runtime identity or proof
that the referenced requirements justify the choice.

After reviewing decisions, the coordinator copies the current
`status.team.basis_digest` from `scenario-context` (or
`scenario.team.basis_digest` from `doctor`) into `basis_digest`. It binds the
selection to the spec, answer history and manifest. A changed input makes the
selection stale; review affected choices before refreshing the digest. The
record starts empty and never pretends that agents have been assigned.

Missing roles or `pending` decisions keep team selection pending. Required
responsibilities cannot be inactive; absent host capabilities are not a scope
exemption. Conditional applicability remains a host judgment: the validator
cannot determine from prose whether a specialist is needed. It also cannot
prove that separate assignee labels represent separate contexts. Keep missing
independent evaluation pending even if inventing another label could pass a
structural check. Normal controller evidence rules still apply.

## Existing setup preservation

Existing files are preserved, including `AGENTS.md`, custom role instructions,
contracts and prior answers. Initialization never appends to an existing
agent instruction file. Explicitly reading `.loop/start.md` is the reliable
entrypoint even when `.loop/agent-instructions.md` already existed. Repeating
init fills missing preset files. Supplying a spec can fill an empty spec file;
a different nonempty spec is rejected before writes. Edit that requirement
file explicitly when changing the project intent.

New initialization uses the current sixteen-role library and team selection.
Reinitialization preserves an existing installed roster and workflow as well as custom role
instructions; it does not silently upgrade a project's team or an active run.
To adopt new roles in an existing setup, have the coordinator explicitly merge
the current preset's roster/workflow with local customizations and prepare a
new reviewed task when frozen inputs change.

Installed manifests without `team_selection` retain their earlier behavior
and do not require the new record/schema. Adopting selection explicitly also
requires activation rules for every installed role. Init may fill new library
files, but preserves the installed manifest, selection and custom instructions.

The preset cannot be combined with `init --native` or added by init to a
project that already has `.loop/engine.json`. Native direct inference remains
a separate single-task route. An individual prepared task can use an existing
controller/evaluator integration, but that does not schedule the host team.

## Checked host context and readiness

```bash
.venv/bin/python scripts/loop.py doctor /absolute/path/to/project
.venv/bin/python scripts/loop.py scenario-context /absolute/path/to/project
.venv/bin/python scripts/loop.py scenario-context /absolute/path/to/project \
  --role backend --output /absolute/path/to/a-new-context.json
```

`doctor` distinguishes valid scenario setup from readiness to start a
controller run. A fresh preset can return `ok: true` and
`ready_to_start: false`: its host must still prepare the task. Ordinary
non-scenario scaffolds retain the original error behavior. The phases are:

| Phase | Meaning |
| --- | --- |
| `AWAITING_SPEC` | The spec file is empty |
| `AWAITING_INPUT` | A recorded blocking question has no answer |
| `PLANNING` | The coordinator has not replaced the task scaffold |
| `TEAM_SELECTION` | The task is concrete, but the declared team is unselected, incomplete, pending or stale |
| `TASK_PREPARED` | Inputs are present and the current task is concrete; execution/checks still need validation |

Schema-valid text is not proof that an answer is sufficient or the plan meets
the spec. The coordinator is responsible for that judgment. Nonblocking
questions may remain unanswered. The first implementation conservatively
blocks controller start on *any* unresolved blocking question, even if some
portable planning can continue independently.

For current presets, `scenario.team` reports the current basis, active and
covered roles, inactive roles and pending responsibilities. Team selection can
be inspected while the task remains a scaffold. Invalid role references,
coverage, declarations or missing required files fail setup validation.

`scenario-context` exports required instructions, requirements, answer history,
team selection, plan, handoff, schemas, profile and task for the selected role. Every export
contains the complete parsed roster once under `scenario`, with its original
file hash in `scenario_source`, plus the selected role's full instructions and
shared context. The coordinator reads or exports each specialist's instruction
path when assigning work; duplicating every specialist prompt into its own
context is unnecessary. All role files remain bound by the scenario input
digest, including roles not selected for that export. The result is not an
AgentStep request. The host must still inspect
source files and applicable nested repository instructions. Required context
is never silently truncated: oversize inputs fail with an actionable error.
Output files are created exclusively.

The optional execution plan, schema and guide are not duplicated into ordinary
scenario contexts. `team-start` freezes the prepared plan and contracts separately;
`team-request` combines specialist instructions with a real controller proposal
context. Existing scenario-only and ordinary controller use remains available.

The host coordinator maintains the question record according to the exported
schema. Questions have stable unique IDs, a reason, a blocking flag and an
ordered `answers` array. Append actual user answers; do not manufacture them
or mark a required answer supplied merely because time elapsed. Initialization
and context export preserve this history without dispatching any model.

## Execution and authority

Specialists use only tools and delegation authorized by their host and user.
Parallel writers require isolated workspaces and a designated integrator.
Without those capabilities, serialize writes. If delegation is unavailable,
the host can carry out roles sequentially and must report that mode honestly.
An implementer's self-review cannot satisfy a required independent review.

The coordinator owns shared planning files and cumulative project accounting.
The portable budget is host-maintained. `team-run` accounts for every coordinator,
worker, review and recipient operation, including rework; it does not include
arbitrary calls outside the scheduler. Hard spend limits require supported
provider bounds/pricing; command/Codex costs remain unknown. A task's required
checks still govern its completion. Markdown reports do not create signed
native evaluator evidence. Keep unavailable required evaluation pending.

Controller start freezes a digest of the scenario spec, answer history, team, role
instructions, workflow, schemas and existing root instructions along with the
task/profile. Subsequent changes refuse continued operations under the old
acceptance. Plan/handoff progress can change without invalidating requirements.
After a material input change, reconcile outstanding effects and start a new
reviewed task; scenario-input refresh is not part of native task amendment.

Offline tests cover setup, preservation, boundary rejection, role contexts,
clarifications, selection freshness/coverage/independence declarations, readiness
and a real-check controller handoff. They do not
establish that a host can successfully develop arbitrary specs or that a live
specialist team was dispatched.
