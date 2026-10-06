# macare Loop job review — 5 October 2026

Loop has preserved useful engineering work, actual failures, and review boundaries, but this job exposes gaps in planning, long-running check control, and repair orchestration. More specialist agents alone will not resolve them. The first improvements should make acceptance achievable, keep the control interface responsive, and provide a supported path from a failed gate back to its implementation owner.

## Scope and observation

Reviewed `/Users/super/Documents/ai/macare`, the Loop framework in this checkout, an unchanged disposable copy of the job database and WAL, the existing performance artifacts, and the latest available host conversation. This was a review: no product edits, job control actions, new performance workloads, or live model evaluation were performed. Database recovery, where needed, occurred only in a disposable copy.

State observation: **5 October 2026, 14:55:33 Singapore time (06:55:33 UTC)**.

- Team: `team-22d34201e8ef4358aecde6aaa1719884`, revision 289, ACTIVE / EXECUTION.
- Formal tasks: five COMPLETE, performance RUNNING with its child back in PLANNING, security/code review/acceptance PENDING. R1 is not accepted.
- Specialist records: 44 COLLECTED and two RUNNING across eight professional roles. These are assignment records, not 46 distinct agents or 44 accepted tasks.
- The first full performance command has ended. Its million-file command timed out after 8,818 seconds, approximately 147 minutes. The ten-million-file command never started because the remaining deadline was insufficient. Owned cleanup recorded no errors or unreaped handles.
- The 1,800-second headless component measurement recorded **1.4859% of one logical core**, above the 1% target. It excludes packaged app UI/XPC and does not establish the complete original product budget.
- The user has authorized bounded same-team repair. The job has recorded a task amendment preserving the child and cumulative usage, and is preparing small-scale comparisons of a repair candidate. That is recovery progress, not a passing performance result.

The pasted 17-hour duration describes the host turn, not a 17-hour million-file scan. The original performance wrapper ran for 10,725 seconds overall. Its result remains UNVERIFIED, with the timeout and failure preserved.

## Findings

### 1. P1 — The planned performance producer cannot satisfy its own acceptance gate

[run-performance.py](/Users/super/Documents/ai/macare/validation/r1/run-performance.py:169) unconditionally emits UNVERIFIED for every non-deferred budget. Its executed workloads produce partial headless component results, with no complete native UI, Dock, or interaction producer. [check-performance.py](/Users/super/Documents/ai/macare/validation/r1/check-performance.py:82) requires PASS for every R1-required budget. Even successful completion of both giant scans cannot make this producer satisfy that check.

The project already acknowledges this limitation in [performance-evidence-preflight.md](/Users/super/Documents/ai/macare/docs/r1/performance-evidence-preflight.md:5). The planning defect is admitting this as the performance acceptance path without first assigning the missing producers. The checker correctly refuses incomplete evidence and should remain strict.

**Improve:** represent each required measurement as a contract with its original conditions, evidence producer, executor, environment, expected artifacts, and verification route. Complete the missing product measurement implementations and their offline cases before another expensive formal run. Keep diagnostic component tests separate from acceptance. Small-scale comparisons must never substitute for the original 30-minute, million, or ten-million conditions.

Framework preflight currently checks command executable availability, rather than whether a producer covers its required evidence. A deterministic audit fixture confirmed that a command using `python3` and a nonexistent producer file is marked `automatic_available=true`; see [native_preflight.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_preflight.py:45). Executable routing should be supplemented by declared evidence capability checks.

### 2. P1 — Required native GUI acceptance has no connected automatic execution route

The cached progress page routes `native-interaction` to external signed import, expressly stating that a signing authority does not prove an evaluator is connected. [execution-tools.json](/Users/super/Documents/ai/macare/.loop/execution-tools.json:10) has no executors, and [r1-acceptance.json](/Users/super/Documents/ai/macare/.loop/tasks/r1-acceptance.json:643) requires interaction evidence.

Security, implementation review, and requirements review show registered review routes. Those reviews do not replace actual native UI interaction. There is no evidence in this audit of a connected external GUI evaluator; external signed import remains possible if one is actually supplied.

**Improve:** setup must either connect and verify a supported native interaction executor or establish a concrete human evaluation procedure and signed evidence intake. Show this requirement before promising automatic end-to-end acceptance. Continue authorized engineering while clearly identifying the acceptance dependency. Never convert an advisory agent report into a native GUI pass.

### 3. P1 — Long checks occupy the MCP request loop and leave continuation ambiguous

[desktop_mcp.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/desktop_mcp.py:105) executes a tool handler synchronously before reading the next request. Workbench submission runs the original checks through that handler. A three-hour check therefore occupies this connection's control path. The host conversation explicitly reported that the Loop interface was occupied; one progress call took approximately 153 seconds near the end of the check.

The browser has a separate refresh thread, so this finding does not mean all dashboard HTTP requests stop. It can refresh the last recorded state without exposing useful within-check progress.

[native_flow.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_flow.py:40) returns a pending-effect action with no tool and `continue_work=false` for an outstanding process. A deterministic fixture reproduced this. A host following the continuation instruction has no bound operation-status action and may finish its turn with an active check outstanding.

**Improve:** make check admission return a durable operation ID quickly. Run the bounded process outside the RPC request loop, expose short status/wait operations and responsive pause/cancel, and reconcile the same operation after reconnect. Continuation should describe the running operation, deadline, available safe work, and next collection action. Preserve leases, cancellation, ownership, and crash journals; concurrency must not weaken these protections.

### 4. P1 — Verification failure was not planned with an executable product repair path

The original performance task allowed validation/report edits but excluded the product sources needing optimization. The first check consumed roughly 10,733 of its original 14,400 controller seconds, leaving insufficient reserve for a comparable complete rerun plus diagnosis. The job consequently prepared product changes under a validation repair-candidate directory and developed a project-local team amendment adapter.

The user subsequently approved the additional paths and bounded execution allowance. The recorded amendment expanded five source/test paths, raised Task6's cumulative limit to 28,800 seconds and team policy to 43,200 seconds, and preserved the original criteria, child and already-used allowance. This resolves the immediate authorization issue; the supported native MCP tool set still lacks a general team-level amendment/rework entry point. The existing child-level amendment and unstarted-stage repair are narrower facilities.

**Improve:** define repair ownership and permitted paths during planning. Add a supported, narrowly validated same-team rework/amendment protocol with an explicit reviewed delta when scopes or limits change. Preserve failed evidence and cumulative usage, reconcile pending effects, invalidate affected candidate evidence, and require final checks on the repaired candidate. Do not require application agents to implement their own framework recovery adapter during delivery.

### 5. P2 — Coarse task boundaries and uniform deadlines concentrate integration in the main thread

[workflow-tasks.json](/Users/super/Documents/ai/macare/.loop/workflow-tasks.json:72) puts the entire native UI after storage/actions. Security and code review depend on performance. Native formal admission is serial even for independently ready tasks; [team_engine.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/team_engine.py:239). Simply removing a dependency will not introduce formal parallelism.

The job did delegate extensively. Several worker receipts returned incomplete compilation or local validation for main-thread integration, and 34 of the 44 collected assignments were collected after their 600-second deadline. Collection timing does not establish when a writer stopped: some explicitly stopped on time or waited for approval. It does show that a deadline alone does not explain the delivery delay or ensure prompt collection.

**Improve:** split work into independently buildable slices with agreed interfaces, exact file ownership, local checks and explicit handoffs. Collect returned work promptly and refill only genuinely ready assignments. Use the current model of parallel disjoint assignments within an accepted workbench; supporting isolated concurrent formal work is a separate framework feature. Conduct useful security/architecture consultation early, before long measurements. Final independent reviews must remain bound to the final candidate, and benchmark phases must remain free of competing loads or candidate edits.

### 6. P2 — The dashboard reports task counts but lacks the information needed to explain elapsed time

[ObservatoryValidation/main.swift](/Users/super/Documents/ai/macare/Sources/ObservatoryValidation/main.swift:103) constructs the fixture and scans it before producing its final result. It does not publish structured fixture/scan stage progress. On failure it exits before returning a final scan summary. [native_host.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_host.py:659) renders formal task and worker states without benchmark counts, throughput or last measurement progress.

The native UI workbench-to-handoff interval was **42,025 seconds, 11h 40m 25s**, while that child's ledger records **58 seconds of controller work**. The interval includes host work and potentially approvals, disconnects, and waiting; it cannot be treated as coding CPU time. Native host token/cost usage is explicitly unknown, and the framework does not enforce aggregate native dispatch/rework/wait totals against the automatic team ledger; see [native_workers.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_workers.py:259).

**Improve:** show current substage, actual agent and owner, last observed progress, processed entries, throughput, raw artifact location, deadline, failure/recovery reason, and the next transition. Separate job elapsed time, host waiting, recorded controller time, and unknown provider usage. Label each policy limit as enforced, supervisory, or unavailable. Show active work prominently and keep collected history accessible without overwhelming the current view. Use measured counters and trends without inventing percentage complete or an ETA.

### 7. P2 — Project instructions and installed skill lag the active milestone and framework

[AGENTS.md](/Users/super/Documents/ai/macare/AGENTS.md:18) still identifies the current batch as R0, while the user and the active task contracts authorize R1. Direct user authorization takes precedence, so this does not revoke R1. It does increase ambiguity on reconnect or when a new worker reads project instructions. The installed Loop skill also predates the current framework guidance on stage repair, execution preflight, and registered review recovery.

**Improve:** store a versioned active milestone and decisions reference, identify historical instructions as historical, and give workers the current precedence and scope explicitly. Add framework/skill/capability version checks at setup and reconnect, with a reviewed migration path between batches. Preserve frozen sources and accepted evidence; do not silently replace an installed skill during a running batch. Reconcile superseded performance preflight notes with the currently implemented oracle so agents do not repeatedly rediscover resolved conditions.

## Application performance follow-up

The component CPU excess and the scan timeout are real signals requiring macare diagnosis. Source inspection identifies repeated statement preparation/per-entry index maintenance and pre/post-transaction WAL checkpoints as possible costs; [StoreSchema.swift](/Users/super/Documents/ai/macare/Sources/HistoryStore/StoreSchema.swift:204). These are candidates for profiling, not proven causes of the measured timeout.

The root scan total is already a single cached-row read; [HistoryStore.swift](/Users/super/Documents/ai/macare/Sources/HistoryStore/HistoryStore.swift:523). An old full-table aggregation explanation would be incorrect for this candidate.

The current recovery's small-scale serial comparisons are appropriate diagnostics. Record fixture construction separately from scanning, compare old and repaired candidates with the same driver, preserve durability and accounting correctness, and run meaningful regressions. Only then rerun the original workloads. A faster component still needs the missing complete product evidence before R1 acceptance.

## Implementation order and offline acceptance cases

|Order|Improvement|Evidence required before live evaluation|
|---|---|---|
|1|Acceptance capability plan: producers, executors, original conditions, required artifacts|Missing producer/UI route stays visible; complete deterministic fixtures cover each required contract; partial evidence cannot satisfy the full gate|
|2|Durable asynchronous check operations and progress|A long fake process leaves status/control responsive; pause/cancel and reconnect reconcile the same operation; no duplicate launch or lost usage|
|3|Supported repair and bounded amendment|Preserves team/child/history/used allowance; refuses unauthorized deltas; survives each crash boundary; new candidate invalidates affected evidence|
|4|Smaller specialist assignments and scheduling|Disjoint writers proceed within capacity; ready drafts collect promptly; expired/stale writers are reconciled; interface conflicts cannot silently integrate|
|5|Useful dashboard and honest limit reporting|Shows actual substage/counters/freshness; distinguishes elapsed/controller/host time; unknown spend stays unknown; each limit's enforcement is explicit|
|6|Versioned deployment and milestone context|Old skill/instruction mismatches surface early; frozen runs cannot be silently upgraded; reconnect preserves actual decisions|

Follow this repository's implementation-first policy: integrate changes and deterministic offline coverage before any new live agent/provider evaluation. Then run the full offline validation and `python3 scripts/check_readiness.py --for-live-evaluation`. Readiness alone does not establish runtime containment or host-model orchestration quality.

## What to preserve

Keep the stable team and durable history, original product standards, separate specialist drafts, serial controlled integration, candidate/source/raw bindings, owned workload cleanup, explicit user scope decisions, and independent final gates. R1 has delivered substantial engineering work; the remaining improvement is making the path to verified acceptance operationally complete and observable.

## Audit evidence

The selected state and event observations are recorded in [macare-loop-job-2026-10-05.snapshot.json](macare-loop-job-2026-10-05.snapshot.json). The original performance result is [run.json](/private/tmp/macare-r1-performance/run-20261005T031333-d82047c0/run.json), SHA-256 `8006a2bf78d06c1dc51545530554170fe2be5125641c85466c7f934e58ac53fa` at review time. Temporary evidence paths may expire.

Two deterministic function-level audit fixtures reproduced the pending-process continuation and executable-only command routing findings. No full test-suite run was necessary for this documentation-only review, and existing tests or advisory agent claims were not treated as fresh product acceptance.
