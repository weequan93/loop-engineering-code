# agent-fabric Loop review — 5 October 2026

Loop needs a more complete verification-and-repair workflow before this kind of project can reliably continue to acceptance. Adding more role descriptions is not the main improvement. The current job has delivered substantial code, but cross-client verification, evidence refresh and recovery from final review still concentrate work in the main conversation.

## Scope and observed state

Read-only review of `/Users/super/Documents/ai/agent-fabric`, its Loop state, the conversation **使用 $loop_engineering 阅读文档**, and this framework checkout. No project files, running team controls, installed skills or provider configuration were changed. No live coding-agent/model evaluation was started.

The database snapshot at **2026-10-05 08:11:02 UTC / 16:11:02 Singapore** recorded:

- Team `team-b8733841c4e64668b730d6e5f132581d`, revision 170, ACTIVE.
- Six of eight formal tasks complete. `g1_local_verify` is RUNNING, with its child still PLANNING and awaiting a submitted step; `g1_local_final` is PENDING.
- All 15 recorded specialist assignments are COLLECTED. They span five roles and three recorded host-agent identities; they are not 15 distinct agents.
- No pending child check process or edit was recorded. The draft verification workbench remains unfinished.
- The latest retrieved host turn contains **235 computer-use tool calls**. This is a call count, not an action count or a time measurement. Its UI work is largely outside the formal child-operation ledger.

The chat service reported the conversation as not loaded and its latest turn as interrupted. That does **not** establish whether a CLI or another host process is still working. This review does not declare the job abandoned or the product accepted.

## Findings and improvements

### 1. P1 — Final review cannot automatically return work to its completed repair owner

The [workflow](/Users/super/Documents/ai/agent-fabric/.loop/workflow-tasks.json:183) makes final acceptance depend on completed verification, then sets `repair_task` to `g1_local_verify`. This is a sensible ownership choice: verification has authorized product repair paths, whereas the final task primarily writes acceptance artifacts.

However, [native_preflight.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_preflight.py:133) marks a completed repair owner as requiring a reviewed plan. [native_flow.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_flow.py:78) only offers automatic repair when the failed task is its own running repair owner. A final-review failure therefore produces findings but no executable continuation into the completed verification task. Existing unstarted-stage repair does not close this gap.

**Improve:** introduce a bounded native rework transition authorized during planning. Preserve task identities, original criteria, history and cumulative usage; reopen only the authorized owner and affected dependents, reconcile old writers, retain failed reviews, and invalidate affected evidence. Scope or budget increases still need an actual approved delta. Ordinary repairs within the accepted envelope should continue without another generic “continue” request.

**Offline acceptance:** a failed final review reopens its completed owner, applies a permitted repair, re-verifies impacted work and obtains new independent reviews. Crash/reconnect, exhausted allowance, stale writers and out-of-scope edits must fail safely without resetting history.

### 2. P1 — Cross-client verification needs durable executable steps and an early capability probe

The required UI coverage is 12 flows across browser, Electron and Android emulator. The host conversation records real client work, including a React Native transport incompatibility and emulator interaction difficulties. Yet Loop mainly sees one unfinished verification task. The draft UI checker validates supplied observations and artifacts; it does not execute the client journey. The project's [execution-tools configuration](/Users/super/Documents/ai/agent-fabric/.loop/execution-tools.json:10) has no registered interaction executors.

The [protected checker](/Users/super/Documents/ai/agent-fabric/g1-local/scripts/check.mjs:69) correctly requires current source binding, actual client steps and matching artifact bytes. This is useful validation, but textual executor identity plus hashes does not independently establish who performed each action. Missing UI evidence remains pending; this review found no basis to call the unfinished draft a false pass.

**Improve:** add a durable verification manifest with client, scenario, executor, preconditions, owner, attempt, observed result and artifacts. Support an honest host-observation bridge where direct automation is unavailable. Record evidence as each scenario finishes and resume from a reconciled checkpoint; do not blindly replay actions with side effects. Distinguish host-reported observations from independently collected evidence.

Before substantial implementation, probe each required surface through **boot/launch → install if needed → login → one actual backend round trip**. Finding an SDK or a runnable command is insufficient. Separate a fixable engineering gap from a genuinely missing user decision.

**Offline acceptance:** fake browser/desktop/mobile transports exercise partial progress, interruption, lost acknowledgements, changed candidates, missing capabilities and non-replayable actions. Unexecuted or merely declared observations never pass a required gate.

### 3. P2 — One global evidence digest causes unnecessary full reruns

[candidateScopeDigest()](/Users/super/Documents/ai/agent-fabric/g1-local/scripts/check.mjs:60) combines declared files across every implementation stage. Both UI and load evidence must match it. Consequently, a mobile-only fix invalidates a prior backend load result and the other client observations too. The host explicitly describes repeating all three clients after its mobile repair.

The inspected draft load report contains 100 measured requests, zero errors and p95 approximately 20.77 ms, but its candidate digest differs from the inspected draft's current digest. It is historical evidence, not current acceptance. The existing rejection of that mismatch is correct.

**Improve:** bind each measurement to its complete declared dependency closure, toolchain, configuration and relevant environment. Reuse evidence only when those inputs are unchanged and the acceptance contract permits it. Shared API, authorization, schema or transport changes must invalidate every affected client. Keep required final integrated acceptance. Introduce this through a reviewed contract version; do not change this running job's protected checker or relabel an old pass.

**Offline acceptance:** a mobile-only leaf change preserves an unaffected backend measurement, while shared backend/schema changes invalidate all dependent evidence. An undeclared or incomplete dependency closure cannot authorize reuse.

### 4. P2 — Native formal tasks are serial even when the dependency graph has parallel work

Desktop and mobile both become ready after Web in the [current graph](/Users/super/Documents/ai/agent-fabric/.loop/workflow-tasks.json:105). But [team_engine.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/team_engine.py:228) permits concurrent formal admission only in its isolated automation mode. Native host work remains serial at that level; parallelism is currently available among specialist assignments within one workbench.

**Improve:** support isolated native workbenches for independent ready tasks, with explicit file/interface ownership and controlled integration. Keep integration and final evidence reconciliation serial. Review whether mobile really depends on completed Web rather than their shared HTTP/SDK contract. Desktop's dependency on Web is justified by its thin-shell design. Raising `max_parallel` alone will not fix the current formal scheduling restriction.

**Offline acceptance:** two ready disjoint tasks proceed concurrently; conflicting ownership, stale bases and failed dependencies prevent integration. Fresh checks validate the integrated candidate. Assign professionals by required capability, including native-client testing, rather than inflating the role roster.

### 5. P2 — Progress needs host liveness, scenario status and separate elapsed-time measures

The controller reports 6/8, no active specialist assignments and a PLANNING verification child, while the retrieved conversation contains extensive manual UI work. The seven child ledgers sum to about 107 rounded controller seconds; that excludes the host's coding, UI work, waits and model usage. It is not the duration or cost of this job.

[native_workers.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_workers.py:257) explicitly does not enforce aggregate native dispatch/rework/wait accounting. Ten of 15 assignments were collected after their 600-second deadlines. Collection timing does not prove a worker kept writing after expiry, but the interface needs to distinguish worker execution from delayed collection.

**Improve:** show “Android verification: scenario X/Y, owner, last observation, next transition” alongside formal task counts. Track controller state separately from a timestamped host heartbeat, active verification session, waiting reason and connection freshness. Show job elapsed time, recorded controller time, host waiting and unavailable provider usage separately. Label supervisory versus enforced limits. A stale heartbeat should mean “host activity unknown,” not “work completed” or permission to duplicate the operation.

**Offline acceptance:** disconnect, long UI work, returned-but-uncollected workers and expired heartbeats produce accurate status without invented percentages, automatic duplicate dispatch or false completion.

### 6. P2 — Preflight must include effective review timeouts, and projects need a version handshake

The [local plan](/Users/super/Documents/ai/agent-fabric/docs/planning/g1-local-plan.md:22) declares three independent reviews at 600 seconds each. The [final contract](/Users/super/Documents/ai/agent-fabric/.loop/tasks/g1_local_final.json:170) allows 2,400 seconds total, with two command checks at 300 seconds each. Those declared maxima consume the whole allowance before setup or repair reserve. Actual commands may finish quickly; this is a planning risk, not evidence that this final task has already timed out.

The framework's new [reserve validation](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_readiness.py:35) sums timeouts embedded in check contracts. These review checks have no embedded timeout; their execution limit comes from reviewer registration. Additionally, [evaluation completion](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/native_host.py:523) invokes verification, so the estimate must reflect actual command rechecks rather than assuming each check always runs once.

The project still has an older installed Loop skill, without `loop_plan_preflight`, operation-status or plan-repair guidance. All eight task contracts omit `extensions.verification_plan`. The latest framework improvements therefore are not enough to establish that this running project uses the new continuation protocol. A CLI invocation of the current server path also does not prove an already-running server has reloaded it.

**Improve:** calculate the effective verification critical path from actual executor limits, setup and rerun policy, then reserve a bounded repair allowance before freezing. Add a framework/skill/schema/capability handshake at setup and reconnect. Migrate at an explicit safe boundary, preserving active decisions, frozen inputs and evidence. Do not silently replace instructions or contracts in this running batch.

**Offline acceptance:** registered review timeouts cannot disappear from budget checks; repeated verification is accounted for; incompatible clients are reported before dispatch; a compatible migration preserves the team's accepted scope and history.

## Early questions and autonomous continuation

The current run already retained the user's local-only scope, synthetic data, local identities and prohibition on paid API/cloud execution. Those decisions should not be asked again. Front-load only unresolved decisions that change delivery scope, permissions, resources, acceptance environments or spending. Capture an explicit repair and continuation envelope at the same time.

Then let the coordinator resolve technical implementation choices, repair failed checks and advance dependencies within that envelope. Capability failures should become engineering work or an early, specific blocker. New external authorization still requires the user; no system can guarantee every later discovery was knowable at intake. Completion must continue to mean accepted current evidence, not that all agents returned.

## Implementation sequence

1. Close completed-owner review recovery and add durable verification scenarios. These address the main continuity gaps.
2. Add early capability probes, effective review budgets and actionable progress to that same workflow.
3. Introduce dependency-aware evidence reuse, then isolated native DAG scheduling. Both require careful invalidation and integration tests.
4. Roll out the skill/version handshake and supported migration before trying the new workflow in existing projects.

Keep implementation and deterministic offline coverage first, following [AGENTS.md](/Users/super/Documents/ai/loop-engineering-code/AGENTS.md). Update implementation inventory only after integrated cases pass. Before any future live evaluation, run the full offline validation and `python3 scripts/check_readiness.py --for-live-evaluation`; this review itself does not authorize or start one.

A secondary storage observation: the state database is about 318 MiB, with about 92 MB of serialized team-event payloads. [team_store.py](/Users/super/Documents/ai/loop-engineering-code/loop_engineering/team_store.py:62) stores full signed checkpoints per event. Investigate verified checkpoint/delta retention later; size alone does not prove a performance bottleneck, and audit integrity must be preserved.

## Validation and evidence

The selected observations, hashes and limitations are in [the review snapshot](agent-fabric-loop-job-2026-10-05.snapshot.json). No remote connection details, credentials or full conversation contents were copied.

The existing deterministic regression `NativePreflightTests.test_cached_failure_routes_actual_findings_to_frozen_owner_and_does_not_repeat_review` passed in 4.819 seconds. Together with source inspection, it confirms the current failed-review routing behavior; it does not implement the proposed recovery feature. No full suite or product acceptance run was performed for this read-only review.
