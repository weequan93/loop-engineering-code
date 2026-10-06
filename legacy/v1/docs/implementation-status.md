# Implementation completion and validation scope

The [native autonomy update](native-autonomy.md) adds opt-in concurrent drafts with journaled refresh and serial checks, dependency-bound host observation reuse, expiring activity reports, effective review budgets and a framework/instruction handshake. It preserves the supervised boundary and original final evidence gate; real host and device/provider behavior require separate evaluation.

The implementation inventory covers **19 required components**, including the development preset, continuous supervised team execution and the [persistent native host supervisor](native-supervisor.zh-CN.md). Each component is counted as complete only after its rules, integrated code and offline coverage pass. [How to use](how-to-use.md) covers project setup and operation; [native rules](native-engine.md) map enforcement to code/tests; [scenario presets](scenarios.md) describe the spec-first route. The [native App/CLI entry](native-host.zh-CN.md) keeps dialogue and native delegation in the host, with project-scoped MCP tools and actual progress display.

Live evaluation follows implementation and full offline validation. The inventory does not start a provider automatically. Separate reports state any actual live checks and their limits; representative coding performance and a deployed Docker boundary remain unverified. Local mode retains operator supervision; unavailable required capabilities refuse admission.

## What is implemented

The supervisor supports an operator-authorized official approval route at a collected checkpoint. Exact supervisor revision and current candidate bind the signed update; original session, lifetime, usage, failures and task contracts remain. A bounded CLI-help preflight rejects unsupported routes before model dispatch and does not attest actual execution permission. Verification producers must still pass their original runtime and owned-cleanup guards. The default remains `never`; official automatic review keeps `workspace-write` and decides individual requests. See [runtime routing](native-supervisor.zh-CN.md#原验证环境的审批接入).

Serial native tasks now have [published instruction recovery](native-instruction-recovery.md). Exact maintained skill upgrades can reconcile stopped drafts through an authenticated reservation, preserving original child/contracts, scopes, waiting deadlines and cumulative limits. Fresh context is metered; original checks/reviews remain mandatory. Unrelated source changes, customized instructions and unresolved writers/effects refuse. Offline validation covers original specialist collection, actual final checks and crash replay; real product acceptance remains separate.

An explicitly started POSIX Codex CLI supervisor provides execution beyond the
current native chat turn. It launches a dedicated session, persists its ID and
resumes it after completed checkpoints, waits for actual questions/evidence and
follows registered project successors. It does not wake the original App chat.
Authenticated lifecycle and dispatch journals retain original lifetime, turn
counts, known event usage and unresolved effects; a project identity lock rejects
concurrent supervisors. Pause/cancel stops the owned CLI process group, while
other detached processes and controller operations retain their original effect
reconciliation rules. Unknown usage/cost remains unknown. Offline tests exercise
real local host subprocesses and command checks with deterministic transport,
including background execution; actual Codex login/MCP/delegation adherence
still needs separate live validation.

Collected CLI supervisor checkpoints can now journal a bounded framework
reconnect and replace the process with the same original supervisor identity,
session, lifetime and accounting. Pause, expiry and unresolved dispatches retain
priority. Fixed-locale process observation accepts the two previously recorded
English date layouts without accepting a different birth time or process group.
Offline tests include an actual process replacement into a fresh CLI with a fake
host and original command checks; this does not certify live model semantics.

Rejected native handoffs now have an explicit scoped recovery route under the
same frozen `native_rework` allowance as independent-review reworks. It verifies
the current rejected receipt, source/environment/check artifacts and authorized
owner, journals original records, reopens original children without new usage
or limits and requires a changed candidate, original checks and fresh acceptance.
The native next-action route and persistent supervisor continue this recovery.
Receipt findings retain their host-declared status and are excluded from
independent-review memory. Crash replay, stop signals, active effects and
exhausted allowances retain their original gates; no real project pass is
implied by the offline fixtures. See [recovery rules](native-rework-verification.md#rejected-handoff-recovery).

| Area | Delivered code |
| --- | --- |
| Contracts and completion | Strict task/profile/proposal/context/action shapes, observable criteria, current-snapshot all-check gate |
| Effects and runtime | Typed role/lease/scope/check broker, version-negotiated text/binary/deletion edits, bounded owned processes, supervised dependency setup, protected named-container backend and deterministic capability probes |
| Evidence | Private purpose/role HMAC authorities, actual command artifacts, registered browser assertions/captures and HTTP samples/thresholds, review/human/interaction/artifact request/sign/import, revocation and replay rejection |
| Budgets | Atomic managed-inference admission, output/pricing bounds, final headroom, actual/unknown usage separation, held ambiguity across crashes/retries/model switches |
| Lifecycle/workflows | Pause/cancel signals, cumulative amendments, capability-checked model switching, sequential stage DAG and final integrated verification |
| Durability/context | Content-addressed restoration, file/process/container reconciliation, signed projection replay/rebuild, workspace/fenced writers, mandatory instructions/findings retained under context limits |
| Integration | File bridge, supervised command/Codex bridges, two direct text-provider drivers, offline protocol/HTTP/fault tests and both end-to-end scripted demos |
| Scenario onboarding | Sixteen-role library, activation/coverage rules, spec-bound host team selection, requirement-to-acceptance handoffs, persistent clarifications, bounded role contexts, setup/task readiness and controller input binding |
| Team execution | Spec-driven coordinator intake, actual answers, continuous dependency scheduling, isolated parallel copies and journaled serial integration, fresh integrated checks/reviews, automatic bounded repair, cumulative team reservations, recipient receipts, cancellation and crash recovery; prepared manual route retained |

The [team memory rules](team-memory.zh-CN.md) extend this durability into every
role dispatch. Integration and offline coverage are recorded in `context_handoff`
and `team_execution`: actual answers, relevant task/check/handoff indexes,
invalidated repair history, source-linked reports and frozen intake contexts
survive restart without extra summarization calls. Received stage responses are
reused after processing crashes; failures retain their cumulative accounting.

Direct provider drivers currently cover OpenAI Responses and Anthropic Messages; other models use the portable/file/command bridge. Claude Desktop has a [project-scoped native MCP bridge](desktop-mcp.md) for queued proposals and actual answers, with a separate Codex reviewer. Evaluator interchange and supervised registered browser/HTTP executors are implemented; human judgment and other artifact/scan procedures require their actual registered evaluator. Opt-in AgentStep 0.3 handles deletion/binary proposals, and frozen dependency setup prepares verified check copies. Live streaming, mode proposals, Git-worktree/submodule provisioning, public-key federation, distributed leases, release operations and arbitrary tool plugins are outside this scope. Continuous scheduling and Desktop chat remain supervised.

The selected host performs semantic spec interpretation. [Continuous team execution](team-execution.md) supplies actual coordinator/worker/reviewer/recipient dispatch, cumulative accounting and isolated copies, rather than counting role files alone. Independent code reviews use separate requests and registered signatures; human/UI/artifact/load evidence stays pending without the actual matching executor. Browser and bounded GET workload plans can be configured before intake; the coordinator receives their actual check IDs and types. Hard spend caps require verified provider bounds/pricing and cover scheduler-owned calls within that team. Generic command/Codex hosts refuse hard spend caps; their cost remains unknown. The manual serial route retains per-child accounting.

## Inventory declarations

[implementation-status.json](implementation-status.json) records each required component's `definition`, `implementation`, `offline_coverage`, and actual source/test references.

| Field | Meaning of complete |
| --- | --- |
| `definition` | Contracts, ownership, authority, failures, compatibility and boundaries specified |
| `implementation` | Integrated code enforces supported rules and refuses unsupported required paths |
| `offline_coverage` | Deterministic success/failure/boundary/recovery cases cover the component and integration |

These declarations are reviewed against code and current offline results. [The recorded offline validation](offline-validation.json) records the full suite and schema/demo checks, including the scenario cases. The checker validates status/reference integrity; it cannot prove correctness, run tests, authenticate a deployed runtime or measure a live model.

Run the report or guarded prerequisite:

```bash
python3 scripts/check_readiness.py
python3 scripts/check_readiness.py --for-live-evaluation
```

A complete inventory returns `0`. An unfinished required item makes the guarded command return `1`; malformed/omitted/duplicate requirements, invalid statuses or missing/escaping references return `2`. There is no force option. Even with `0`, a current full offline validation and explicit operator dispatch remain prerequisites. CI checks the inventory without calling a model.

## Separate remaining validation

1. **Deployment conformance:** verify the selected immutable image/daemon/platform, actual check dependencies, containment controls, cancellation and reconciliation. Docker access was unavailable to this development session; fixture tests do not certify that deployment.
2. **Live backend conformance:** explicitly verify selected model IDs/API behavior, counts/output bounds, prices, usage coverage, failures/cancellation and fresh-context switching. A small CLI team check does not certify the direct-provider or alternate-host integrations.
3. **Representative task evaluation:** compare accepted outcomes, false completion, regressions, time/spend/unknown coverage and interventions on a fixed corpus. The bundled small Python task is not a performance benchmark.

Those are validation of actual services/deployments and performance, not missing 1.0 code definitions. Their absence prevents claiming production readiness for a particular unattended deployment. The implementation remains usable through the documented supervised and offline paths.

The [local collaboration corpus](offline-corpus.json) executes persistence/API/
HTML, injection/escaping checks, a measured local workload and actual interface
regression recovery across seven roles. Its proposals and independent judgments
are deterministic fixtures; it is not representative live-model performance.
The [reinitialization guide](reinitialize.zh-CN.md) documents archived settings,
unchanged coding history and refusal of unresolved operations.

The [specialist agent guide](development-agents.zh-CN.md) describes professional
procedures for all sixteen roles and the shared work/evidence protocol. Current
coverage is resolved into owner and recipient contexts; scoped workers receive
their task role and covering owner, while independent reviews retain their role.
Declared protocol changes invalidate frozen runs. New development profile sizing
is explicit in the preset and preserves existing profiles during ordinary init.

Dependency preparation, registered browser assertions/captures, bounded HTTP workload checks and opt-in binary/deletion proposals are covered in the [执行工具与重新设置指南](execution-tools.zh-CN.md)。

The [setup requirements assignment](requirements-setup.zh-CN.md) reuses the
requirements role in a dedicated pre-development request. It reads bounded
project documents, collects actual answers, journals spec/draft writes and stops
at the prepared-spec handoff. Empty-spec team runs enter this phase instead of
rejecting the user before intake. No new role or automatic live dispatch is
created by initialization; semantic spec quality still requires real evaluation.

The native App/CLI route derives explicit next actions and prepares the next
bound stage/task packet with `loop_next`. Typed plan submission retains the
same admission/evidence gates. Actual planning assignments can be recorded
separately from verified task completion, and multiple human answers retain
the waiting state until the final required answer. Host instructions require
continued work through submissions, checks and handoffs. The MCP server cannot
wake a host that has ended its turn; actual native-model adherence remains a
separate evaluation. Offline cases cover the full document/answer/plan/task/
check/handoff path and refusal at real stop conditions.

Native plans cross-validate each task against its engine stage graph before
freezing. Omitted staged engines receive one stage owning all final criteria;
explicit invalid graphs are rejected and valid declared stages retain their
ordering when an independent assessment is appended. Old empty-stage plans
can use the bounded `loop_repair_plan` journal only before a child was created.
It preserves accepted task contracts, limits, task IDs, team dependencies and
completed deliveries. Crash replay accepts only the original/new engine bytes;
changed inputs, source conflicts, started children and stop signals refuse it.

Native coding workers can draft real files in a disposable, request-bound copy.
Collection derives versioned changes and retains the original scopes, frozen
candidate, checks and evaluator gates. Recorded host waiting deadlines never
renew through polling; expiry requires the active host to inspect preserved
files and take over or boundedly reassign in the same cumulative child run.
The copy shares host OS authority and is not runtime containment. Loop cannot
interrupt a native agent by itself. Native waiting is a supervisory deadline;
host-model time and usage outside controller operations are not in its ledger.
Offline coverage validates actual draft
files and timeout recovery; native host adherence remains separate validation.

The native main thread can split one accepted task into bounded professional
assignments. Each selected specialist receives its role rules, explicit file
ownership and a separate copy of the current coordinating draft. Collected
dependencies gate dispatch, frozen policy and actual host capacity bound
parallelism, and the board links concrete work to reported native agent IDs,
observed files and non-renewing deadlines. Returned drafts import serially
through a frozen crash-recovery journal into the coordinating copy. Pending
specialists prevent parent submission, evaluator admission and final handoff;
the original task checks, independent
evidence and formal dependency gates remain enforced. Opt-in native formal
tasks can now draft concurrently through the native autonomy extension; imports
and checks remain serial. The host still owns spawning,
stopping, replenishing and supervising real agents; offline fixtures do not
certify live-model adherence or independently attest native identities.
Native specialist tools enforce registered parallel slots, individual waiting
deadlines and a 128-record history bound. They do not enforce native model
dispatch/rework totals or total host waiting time against the automatic-route
ledger. A preparation crash before assignment registration can leave an orphan
disposable copy; it launches no model and does not edit the shared project.

Native packets retain the task's original context policy after adding team,
proposal, workbench and specialist metadata. Nested native source excerpts and
retained history are optional; task/state, repository rules and authenticated
findings remain mandatory. Saved agent packets use the same compact UTF-8
representation as the measured interchange limit. Truly oversized mandatory
material fails closed without changing the frozen profile or criteria.

Native verification preflight exposes check IDs/types and exact execution routes
before implementation. Signing-key availability alone is a signed-import route,
not an active evaluator. [Explicit independent review registration](native-review.zh-CN.md)
adds a supervised fresh Codex CLI context while retaining existing contracts,
current candidate/environment bindings, artifacts, signatures, cumulative child
time and bounded attempts. Unknown CLI spend refuses hard caps. Process effects
require reconciliation; saved results replay without another dispatch. Current
failed/inconclusive judgments stop empty evaluation loops and retain actual
findings/repair responsibility. Optional explicit retries preserve prior evidence
and cumulative limits. Registration starts no model; real model quality and
host-level independence/containment remain separate from offline conformance.
Current failed reviews can prepare a bounded repair packet only for that same
running, planning task with existing edit authority and a writable scope. The
packet retains its real signed findings and existing child/workbench deadlines;
it neither retries the review nor reopens completed or differently scoped work.

Registered native reviews also receive complete copies of recorded current,
verified dependency and prior judgment artifacts with exact original bindings.
The evidence manifest and bytes live outside the source candidate, stay within
fixed material limits and are checked before dispatch and after review. Signed
results retain the inspected copies; incomplete, redirected or changed material
cannot silently become a passing judgment. This fixes the gap where a reviewer
had only output tails and inaccessible private-store paths. It adds no live
dispatch, observation allowance, review attempt or budget.

Native background checks, early decision preflight, recovery and project upgrade instructions are documented in [Native coordination through completion](native-continuation.md).

The continuation correction distinguishes unfinished measurement/result-import
engineering from externally blocked acceptance. Actionable task packets retain
that guidance. In frozen parallel plans, task-local evaluator blockers remain
visible while executable independent work is selected; serial admission,
global recovery/stop gates, original evidence and cumulative limits are unchanged.
Offline routing and real local fixture checks cover this selection. The host
still owns sustained execution, and required human judgments remain external.

The native project queue now links explicitly authorized successor milestones
through accepted batches instead of equating one increment's COMPLETE with the
whole product. A new successor enters SPEC/INTAKE with prior answers and original
gates, never replays a completed workflow, and recovers its authenticated link
after lost replies or reconnection. A cumulative controller allowance covers its
root and successors; earlier ledgers remain separate and host spend is unknown.
Preflight/submission reserve child and integration limits. Offline local cases
cover two accepted successors, duplicates/concurrency, pause/cancel, real input,
unadmitted reserved identities, scope/budget refusal and retained evidence.
The queue is host-driven coordination, not an unattended model daemon; complete
requirements coverage and sustained native execution still need actual review
and future live evaluation.

Explicit native project budget amendments append actual host-declared user
authorization and an absolute larger controller cap to the same authenticated
root ledger. The original queue, exact consumed time, per-child limits, checks,
review attempts and effects/spend policy remain unchanged. Stable amendment IDs
and expected prior caps prevent duplicate credit and stale concurrent updates;
paused/cancelled projects refuse new amendments. Preflight exposes the concrete
shortfall and minimum total before freezing; amendments grant no new scope or
external capability. Offline cases cover accounting/evidence preservation,
admission recovery, crashes/reconnects, conflict and missing-accounting refusal.

Native progress now exposes a read-only host turn boundary derived from the
actual next action. Passing command checks, collected specialists and expired
writers still expose their required handoff, remaining work or takeover; real
questions and missing human results remain visible gates. The dashboard and
packets state the distinction. This changes no contract, evidence, accounting or
admission and cannot force a host to keep running or wake an ended conversation.


## Native review recovery and host verification sessions

[Native review recovery and verification sessions](native-rework-verification.md)
adds opt-in completed-owner rework under the frozen workflow/team allowances.
The journal keeps original children, usage, failed evidence and prior handoffs;
affected descendants re-enter verification, and unchanged repair candidates do
not advance. Ordinary terminal resume rules remain unchanged.

Candidate-bound host verification sessions retain scenario dependencies,
observations and captured artifact bytes across reconnects. Unknown, expired,
failed or blocked effects require explicit reconciliation before retry. Attempts
remain consumed across new candidates, and the dashboard exposes per-surface
progress. Reports never become independent evidence or replace original checks.
The host still provides actual interaction tools; formal integration remains
serial and final evidence invalidation remains conservative. Dependency reuse
applies to declared host measurements only. No installed live project
is migrated by these framework changes.
