# Recovery after a published instruction upgrade

An existing serial native task can recover from an exact published Loop skill
upgrade through `loop_workbench_reconcile_instructions`. This does not require
adding `native_parallel` to its frozen plan. The host first uses `dry_run=true`,
inspects the original/new instruction hashes and actual stopped writer records,
and then supplies exactly the reported `reviewed_changes` with `dry_run=false`.
`loop_next` exposes this inspection before trying to collect a stale specialist.

Only `.agents/skills/loop-engineering/SKILL.md` and
`.claude/skills/loop-engineering/SKILL.md` are eligible. The original bytes must
match the maintained published hash inventory; the new bytes must exactly match
the current published template, with unchanged file type and mode. Customized
instructions, deletions, redirects, unrelated source changes, frozen input
changes, active unrelated tasks, running writers, pending imports, checks,
verification effects, child/project stops and exhausted child time refuse.
Claude Desktop control instructions are outside this route and retain frozen
input refusal. This is local supervised reconciliation, not process attestation.

The authenticated reservation journal binds the original request, coordinating
copy and stopped specialist candidates. A replacement copy receives the current
published instruction and original scoped coordinator edits. Original files are
retained. Specialist records are rebound with original request/workbench/base
provenance, keeping their IDs, agent identities, files, scopes, dependencies,
status and deadlines; original stable drafts can then be collected normally.
No specialist is restarted and no source is imported into the shared project
by reconciliation itself. Original checks, independent reviews and handoffs
must still run on the newly integrated candidate.

The same child, contracts, task graph, rejected receipts, review attempts,
verification attempts, cumulative usage and non-renewing host waiting deadline
remain. Fresh-context preparation is metered by the original controller. An
expired wait remains expired and permits only the existing supervised stable
collection/takeover route. Recovery adds neither work authorization nor budget.

The journal reserves the replacement request ID before context creation.
Crashes during copy preparation, request creation or context commit recover
that ID. Exact source and original draft identities are checked again before
commit; unknown changes in a partial recovery copy refuse replay. A lost final
reply replays the same completed record, including after task completion, without
another mutation. Team pause blocks a pending recovery; resumption preserves
its journal and original deadline. The global administrative recovery history
is bounded to four entries and existing workbench history remains bounded to 64.
This bound is independent of product repair/review allowances and grants none.

Validation uses deterministic native-host fixtures, actual scoped file imports,
original local checks, crash injections and signed journal audit. It does not
prove live model adherence, runtime containment or product acceptance.
