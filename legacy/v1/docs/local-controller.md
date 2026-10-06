# Supervised file and command bridge

This is the implemented, operator-supervised path through the native design. It accepts model-neutral JSON proposals, applies guarded file changes, runs actual checks, stores a recoverable candidate, and decides completion. It works with a file bridge, a configured command wrapper, or the Codex CLI proposal driver.

Its effective autonomy is **manual/operator-supervised**, including when a task requests assisted operation. The state records the requested mode and downgrade reason. `unattended` is rejected. The implemented [native engine](native-engine.md) adds reserved inference, registered evaluators and a protected check backend. [How to use](how-to-use.md) covers both routes; a project containing `.loop/engine.json` starts a native run automatically.

## Install and exercise

Use Python 3.11 or newer. From the framework checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/demo_local.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_contracts.py
```

The dependency is `jsonschema`. The decision core remains usable with the standard library alone. The CLI is repository-local; it does not require a plugin or installation into an agent's home directory.

The demo copies [the intentionally buggy application](../examples/slug-project/), establishes a failing baseline, rejects an overbroad fix that breaks compatibility, reconstructs state with a fresh controller, accepts a focused fix, and restores the delivered snapshot. Its proposals are scripted; the check commands and source changes are real. To retain the database and artifacts, pass `--keep` with a fresh directory. `--report` saves the actual summary.

## Initialize a project

The following variables point at your framework checkout and a state directory **outside the target project**. Replace the paths before running:

```bash
LOOP_PYTHON="/path/to/loop-engineering-code/.venv/bin/python"
LOOP_CLI="/path/to/loop-engineering-code/scripts/loop.py"
LOOP_PROJECT="/path/to/your-project"
LOOP_STATE="/path/outside/your-project/loop-state"

"$LOOP_PYTHON" "$LOOP_CLI" init "$LOOP_PROJECT"
"$LOOP_PYTHON" "$LOOP_CLI" doctor "$LOOP_PROJECT"
```

`init` discovers candidate build/test/lint/start commands from common JavaScript, Python, Go, and Rust layouts. It never executes them. It creates missing `.loop/project.json`, `.loop/task.json`, `.loop/handoff.md`, `.loop/agent-instructions.md`, and `LOOP.md`. Existing files and agent instruction files are preserved. Symlink control targets are rejected.

Review the project profile's commands, context paths, conventions, and snapshot exclusions. Detected commands are explicitly unconfirmed; the `confirmed` flag is operator metadata, not a permission grant. Execution uses the approved task's checks, not every discovered command. Replace the task scaffold, choose narrow write scope, protect approved check files, specify meaningful criteria/checks, and remove `extensions.scaffold` before starting. Alternatively, pass an existing valid task via `init --task /path/to/task.json`.

`doctor` validates contracts, checks command availability, and reports missing controls without executing project commands. It reports unsupported spend caps, unavailable required runtime capabilities, verification reserves that leave no implementation time, and non-command checks that need an external evaluator.

## Run with any text-capable agent or model

```bash
"$LOOP_PYTHON" "$LOOP_CLI" start "$LOOP_PROJECT" --state-dir "$LOOP_STATE"
```

This freezes the task/profile, captures the candidate, runs the baseline check plan, and returns a run ID. A failing baseline is expected for a bug. If every required check already passes on the unchanged candidate, the controller completes without an agent call. Exhaustion during baseline produces a stopped budget state. `--no-baseline` is available when establishing it is unnecessary; use it deliberately.

Set the ID returned by `start`, then export a fresh context:

```bash
LOOP_RUN="run-replace-with-returned-id"
"$LOOP_PYTHON" "$LOOP_CLI" context "$LOOP_RUN" --state-dir "$LOOP_STATE" --output /path/to/context.json
```

Give the context and [AgentStep schema](../schemas/step-v0.2.schema.json) to the model. Request **one JSON object**, without Markdown fences, representing the smallest useful proposal. It contains contract/base snapshot digests, criterion IDs, expected observation, full file contents and existing hashes, evidence references, and a next action or explicit blocker. It cannot supply a successful status or redefine a check. The [saved step example](../examples/step-v0.2.json) illustrates structure; its snapshot is illustrative and cannot be replayed into a real run.

Save the response outside the project input snapshot, then submit it:

```bash
"$LOOP_PYTHON" "$LOOP_CLI" step "$LOOP_RUN" --state-dir "$LOOP_STATE" --file /path/to/step.json
"$LOOP_PYTHON" "$LOOP_CLI" status "$LOOP_RUN" --state-dir "$LOOP_STATE" --events
```

The controller rejects stale contracts/snapshots, unknown criteria/evidence, duplicate step IDs, traversal, symlink writes, filesystem spelling aliases, excluded input edits, scope violations, and incorrect file hashes. It validates the entire edit set before writing. `.git` and `.loop`, including case variants, are always protected. Scope patterns use Python `fnmatchcase`: `*` can match `/`, and matching otherwise uses exact case. A new file has `expected_sha256: null`. The default 0.2 protocol writes UTF-8 files. Explicit task `extensions.agent_step_version: "0.3"` selects deletion and canonical base64 binary writes, including hash-guarded recovery and parallel integration. Mode changes remain unsupported. See [execution tools](execution-tools.zh-CN.md).

Accepted act or verification proposals count as attempts. The controller executes the task's checks on a fresh materialization, hashes stdout/stderr, rejects mutated inputs, evaluates all required evidence, and checkpoints the result. `request_verification` merely starts that evaluation. A claimed completion never overrides a failure.

## Command wrappers and Codex CLI

A generic wrapper receives one compact JSON ContextBundle on stdin, executes one bounded model request, and emits one AgentStep JSON object on stdout. Send diagnostics to stderr. It runs in a fresh candidate copy. The controller supplies file-backed stdin so large contexts cannot deadlock a pipe. The entire final response must be strict JSON and at most 2 MiB; no arbitrary shell text is extracted.

```bash
"$LOOP_PYTHON" "$LOOP_CLI" drive "$LOOP_RUN" --state-dir "$LOOP_STATE" \
  --adapter command --argv '["/path/to/python", "/path/to/model_wrapper.py"]' --turns 1 --timeout 120
```

The wrapper can use any provider SDK or existing CLI that can satisfy this contract. Wrapping another coding agent does not make its tool permissions or output protocol identical. A generic command driver declares text input and headless dispatch; other capabilities remain unsupported. Generic token and cost usage remain unknown. [Adapter contracts](adapters.md) describe how to extend a driver without changing the loop's lifecycle.

The concrete Codex driver uses a separate ephemeral invocation, read-only sandbox selection, JSONL events, a schema-constrained final response, and a final-message file:

```bash
"$LOOP_PYTHON" "$LOOP_CLI" drive "$LOOP_RUN" --state-dir "$LOOP_STATE" \
  --adapter codex --turns 1 --timeout 180
```

The command contract was checked against the installed **Codex CLI 0.159.2** help. Authenticate through the CLI's normal login flow before use. The driver ignores the user's general config to avoid inheriting unrelated configured integrations, uses the isolated CLI default model unless `--model` is explicitly provided, and retains execution-policy rules. It asks the model to propose only; the framework broker applies changes. Supported flags can differ across CLI versions. See the official [non-interactive guide](https://developers.openai.com/codex/noninteractive) and [CLI reference](https://developers.openai.com/codex/cli/reference).

Codex usage events normalize input plus output token counts; cached input is not added twice. Missing events, interrupted calls, and any earlier unaccounted attempt make cumulative usage unknown, with a separately reported known subtotal. No hard future-spend reservation is claimed. The CLI adapter is implemented, but a live model run and cross-provider conformance have **not** been established by this repository's scripted tests.

`--turns` bounds this invocation in addition to task-wide limits. A transient dispatch failure gets at most one retry; malformed output gets at most one format-repair attempt. Counts persist across restarts. A one-turn invocation leaves that retry available for the next invocation. Authentication/permission failures wait immediately; missing executables block. Failure classification from CLI error text is heuristic. No check is rerun until a lucky pass under an implicit flakiness policy.

## Resume, cancel, and restore

```bash
"$LOOP_PYTHON" "$LOOP_CLI" resume "$LOOP_RUN" --state-dir "$LOOP_STATE"
"$LOOP_PYTHON" "$LOOP_CLI" verify "$LOOP_RUN" --state-dir "$LOOP_STATE"
"$LOOP_PYTHON" "$LOOP_CLI" cancel "$LOOP_RUN" --state-dir "$LOOP_STATE"
"$LOOP_PYTHON" "$LOOP_CLI" restore "$LOOP_RUN" --state-dir "$LOOP_STATE" --target /path/to/fresh-restoration
```

Resume reloads durable state and preserves every counter. A multi-file edit has a persisted journal containing old/new byte digests. After interruption, the controller rolls forward only paths still matching either approved old or new content. An intervening user change stops recovery before any additional write. A new context export supplies current hashes; old proposals become stale.

Use `runs --state-dir "$LOOP_STATE"` to find saved run IDs, including a start interrupted before its response was returned. The context includes recent consumed step IDs; choose a new ID for each completed proposal.

Process dispatch is journaled before spawn, with PID/identity recorded afterward. Recovery can terminate an owned matching process. A live process without a provable identity, a changed PID identity, or a dispatch interrupted before PID storage requires operator reconciliation. It never treats that gap as proof that nothing ran. Inspect/stop the earlier effect, then record the specific attestation:

```bash
"$LOOP_PYTHON" "$LOOP_CLI" reconcile "$LOOP_RUN" --state-dir "$LOOP_STATE" \
  --action-id process-replace-with-pending-id --note "Describe how the earlier effect was inspected and stopped."
"$LOOP_PYTHON" "$LOOP_CLI" resume "$LOOP_RUN" --state-dir "$LOOP_STATE"
```

The attestation is an operator statement recorded in the journal, not model-written evidence or proof of isolation. A process still observed as the earlier owned/unknown group cannot be attested stopped. Resume after reconciliation requires fresh verification.

Cancellation is an independent database flag, polled during execution. On POSIX the broker terminates its owned process group and waits for the child. Processes that escape that group and external operations require protected-runtime controls. Windows currently terminates the foreground child only; `doctor` does not advertise full group bounding/cancellation there.

Restore writes a fresh directory from hashed blobs; it never overwrites the user's workspace. The saved candidate is the delivery artifact. `status` reports whether the live workspace still matches it. Succeeded, cancelled, failed, and budget-exhausted runs are terminal in this CLI. An authorized revised contract uses a new run; resume never silently increases a budget.

## Limits and trust

Iteration, active wall time, repeated root failures, and stalled progress are controlled locally. New criteria or evidence-backed diagnostic facts count as progress; repeated facts do not. The verification reserve is `max(configured reserve, sum of command-check timeouts)`. Implementation/model dispatch cannot consume it. The final dispatched iteration can still apply its proposal and verify; an interrupted final iteration can resume for reconciliation and checks.

Active preparation/model execution, broker edits, and checks accumulate time with a monotonic clock. Normal waiting time is excluded. A crash leaves unknown execution duration; recovery conservatively charges the full observed wall-clock gap. This compatibility controller does not meter every export/checkpoint overhead. Native top-level operations additionally meter that work once. `max_wall_seconds` is therefore a local active-work limit, not a host-wide resource guarantee. Hard token/cost caps block dispatch even if previous usage is known, because a future request cannot be conservatively reserved.

Snapshots use sorted paths, file modes, sizes, actual bytes, and the frozen exclusion-policy digest. Git status and a commit hash are not substitutes. `.git`/`.loop` control inputs are always omitted. Default exclusions include caches, local dependency directories, and `.env` files. Review them: checks needing excluded dependencies/fixtures require an appropriate explicit policy or provisioned environment. This is not comprehensive secret discovery. The context byte limit applies to compact UTF-8 JSON; the contract/state are never truncated, and omitted source/history counts are reported. Full raw artifacts remain in the store.

The environment digest covers platform, Python runtime, project policy, resolved check executables, frozen tool plans/runtime files and declared dependency input/output bytes and modes. Repository dependency manifests are captured with the tree. Ambient variables, undeclared package closure, remote service state, and containers are not fully identified. Symlinks are captured as identities but verification/restore materialization rejects them; submodule provisioning is not implemented.

The broker validates its own writes, but arbitrary check/wrapper processes share the current OS user's filesystem, network, environment, and privileges. Keeping state outside the project and using fresh copies prevents ordinary proposal writes into the ledger; it does not isolate that ledger from an arbitrary subprocess. Required review, UI, human, and artifact checks remain blocking in this compatibility controller; native runs support registered evaluator request/sign/import procedures. There is no automatic oracle-waiver, merge, deployment, scheduler, parallel-agent topology, or remote recovery service.

SQLite commits state, event and authenticated replay checkpoint together. Per-run locks and same-user physical-workspace locks coordinate writers across state stores and aliases, with monotonic lease fencing. `audit` reconstructs from the signed projection checkpoint stream; `rebuild` advances the fence and preserves unresolved effects. This is not a distributed lease or an event-payload-only reducer. Private host keys authenticate records, but arbitrary local subprocesses still share OS authority. Use the native protected backend only after deployment conformance; see [native rules](native-engine.md).
