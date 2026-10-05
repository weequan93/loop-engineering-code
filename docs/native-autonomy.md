# Native coordination, recovery and compatibility

This extends [bounded rework and host verification](native-rework-verification.md). The active App/CLI host continues the accepted batch through actual next actions. MCP cannot think, wake a finished conversation or attest native agent containment. This update performs no live model/device evaluation or running-project migration.

## Early decisions and effective budgets

Read project documents and prior actual answers during SPEC/INTAKE. Batch unresolved material decisions about scope, acceptance environment, permissions, services/data, delivery and spending. Routine engineering choices need no renewed permission. Unexpected material dependencies may still require later input.

`loop_plan_preflight` returns `effective_budgets`: command timeouts once initially and again after each external evidence import, effective registered review/executor durations, and a separate repair reserve. Unknown external durations stay unknown. `verification_plan.external_timeouts` declares bounded manual/external durations; it cannot underestimate a known registered timeout. New native plans with verification planning refuse known overflow after automatically required role reviews are added. Legacy frozen contracts retain their original rules and expose diagnostics.

This allowance is not a forecast: preparation, host work, staged retries and repairs need additional headroom. Reserve another complete pass when planning a retry. Missing evaluator authority and actual executor availability remain separate checks. Hard native host token/cost caps remain unsupported.

## Concurrent drafts, serial integration

A new workflow can declare `native_parallel: {"max_parallel": 2, "max_refreshes": 3}`. Participating tasks declare `draft_paths`: exact workspace-relative files inside their original write scope and snapshot. Dependencies and phase handoffs still apply. Overlapping, ancestor/descendant and case-equivalent ownership refuse concurrent admission. Tasks without declared ownership retain exclusive admission. Both workflow/team capacity apply; the host must also respect its actual agent slots.

Each task receives a separate disposable workbench. Original child controllers retain the shared project as their integration target. Imports and actual checks run serially under team/workspace ownership; this is not OS isolation. Direct proposals cannot bypass declared ownership. Receive the preceding handoff before another import.

Integration may make another draft stale. Submission refuses it; `loop_workbench_status` lists `project_changed_paths`. After actually inspecting those changes and collecting/stopping the draft's writers, call `loop_workbench_refresh` with its original ID and exact reviewed paths. File preconditions reject conflicts before shared-project writes. The returned copy preserves edits on the current base; the original copy remains available. Read its new context with `loop_workbench_prepare`.

Refresh journals recover after interruption and explicit pause/resume. They retain the original child ID, cumulative counters and host waiting deadline, and consume the frozen refresh allowance. Unknown effects and changed bases refuse replay. Original checks/reviews run on the integrated candidate; final acceptance still depends on every task.

## Reuse unchanged host measurements

Optional `extensions.verification_dependencies` has `schema_version: "1.0"` and `modules`, each with `id`, `paths` and `depends_on`. Scenarios name `input_modules`. The accepted graph must include shared API, schema, authorization, configuration and toolchain dependencies. Every captured file must have exactly one module owner; missing/ambiguous coverage disables reuse. Unknown references and cycles reject the contract.

Bindings include the original contract, scenario, transitive dependency contents, reported environment and controller environment identity. A new session may retain an unchanged observation with its original candidate, attempt and artifact provenance. Missing/corrupt artifacts prevent reuse. Later failed observations supersede earlier passes; scenario prerequisites must also carry forward. Reuse neither consumes a new action attempt nor resets prior attempts. A mobile leaf change can preserve an unrelated backend observation; a shared dependency change invalidates affected scenarios.

This reuses host measurement artifacts, **not formal controller evidence**. Current formal checks and final integrated acceptance remain mandatory. File coverage cannot prove semantic completeness: the host must review the graph and identify actual toolchain/runtime/test-data state in the environment description. Unknown environment state requires fresh measurements. Existing protected project checkers and historical passes are never rewritten.

## Honest activity and compatibility

`loop_heartbeat` records the current host owner, actual activity and optional task, with a 15–300 second lifetime. Only explicit reports renew it. Another owner cannot overwrite a still-current report. It is a host declaration, not process attestation, a worker lease or permission to duplicate/replay effects. Expired/unknown activity requires inspecting the original host and operations. Pause/cancel takes precedence.

Progress separates batch elapsed seconds from its authenticated first checkpoint, recorded controller operation seconds and unknown host usage. Scenario observations, specialist assignments and background operations remain separate facts.

`loop_handshake` reports protocol/features, running-code versus disk fingerprints, and installed instruction status. Changed framework files require MCP reconnection before normal mutations; inspection remains available. Exact known prior instructions are upgradeable; customized instructions are preserved. New opt-in policies belong in a new accepted plan. Active contracts, answers and history are not silently migrated.

Use the existing `host-install` with the project's original state directory and selected host, then reconnect MCP. It installs instructions/configuration, starts no model and does not prove host adherence. See [native setup instructions](native-host.zh-CN.md).

## Offline coverage

`test_native_parallel`, `test_native_session`, `test_verification_dependencies`, `test_native_readiness` and the earlier rework/verification suites cover real local files, deterministic proposals, subprocess checks and crash/pause boundaries. Live evaluation remains subject to the implementation inventory and full offline validation; readiness does not dispatch a provider.
