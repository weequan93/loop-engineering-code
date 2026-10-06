# Coordinator: continuous checked team execution

Users supply a spec and answers. Use the installed framework's Python environment
and scripts/loop.py; keep --state-dir /absolute/private-state outside this project.

Run team-run /absolute/project --adapter codex for an installed authenticated
Codex CLI, or select an actual command/provider host. This explicitly dispatches
bounded work. The coordinator prepares the team, checked task contracts and
workflow-tasks.json from the spec; users need not write task JSON. The scheduler
continues dependencies, actual checks, separate review/recipient requests,
serial integration and bounded repair. Initialization and team-start launch no workers.

Inspect team-status TEAM_ID. Blocking questions appear in questions.json.
Record actual answers with team-answer TEAM_ID --question QUESTION_ID --answer
"Answer", then team-run --team-id TEAM_ID with the same state and selected host.
Do not invent answers. Required human/UI/load/security/artifact procedures need
actual registered executors; an agent verdict cannot fabricate their execution.

Respect team-policy.json: concurrent workers, cumulative dispatch/repair limits,
an absolute deadline including waiting/recovery, and one ledger for coordinator,
worker, reviewer and receipt calls. Hard spend caps need verified provider bounds
and prices; generic command/Codex hosts refuse them. Unknown usage remains unknown.

Task contracts declare role, phase, dependencies, real checks, outputs and
recipients. Final integrated acceptance depends on every other task. Assessment
tasks set repair_task to their implementing dependency. Keep accepted checks and
scope frozen; represent every required part of the spec in acceptance.

Before freezing intake, inspect `.loop/execution-tools.json`: optional supervised
dependency steps and browser/HTTP plans need real installed tools and explicitly
authorized targets. Match each plan's check_id to an interaction (browser) or
artifact (HTTP load) task check. The scheduler executes registered plans and
retains actual observations; missing tools or judgment remain pending. These
local executors do not enable unattended or Docker execution.

Workers propose changes in private snapshot copies. Default AgentStep 0.2 uses
UTF-8; explicit task extensions.agent_step_version 0.3 also permits bounded binary
bytes and hash-guarded deletion. Serial integration refuses conflicts, symlink
and mode changes. Re-run checks
on integrated candidates. Independent roles need native registered review checks;
preparation supplies omitted runtime/evaluator configuration and the mandatory
separate code assessment. Existing checks and required human/UI/load/artifact
procedures stay required. The wrapper performs a separate request and signs its
actual returned assessment.
Recipient receipts cannot replace these results. Local execution remains supervised.

Use team-pause/cancel/resume/audit/runs for lifecycle. Ambiguous dispatch requires
team-reconcile --operation-id OPERATION_ID --note "Actual inspection details";
inspect and stop its old process first. Child effects retain their reconciliation
rules. Reuse identities and cumulative budgets. Changed frozen requirements need
a reviewed new team after stopping/reconciling the old one.

Prepared manual hosts can still use team-start, team-request, actual worker,
team-submit, controller verification, team-collect and actual team-receive.
That route is serial with per-child budgets. Claude Desktop can follow the file
workflow or use the framework's scoped native MCP bridge. Export its configuration
with desktop-config PROJECT --state-dir PRIVATE_STATE, then run team-run with
--adapter desktop --review-adapter codex. Desktop workers are supervised; separate
Codex requests perform independent reviews. Desktop usage remains unknown.
