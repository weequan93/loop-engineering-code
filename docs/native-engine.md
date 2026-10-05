# Native engine 1.0 rules

The implemented engine is `NativeController`, layered on the model-neutral local controller. It owns acceptance, snapshot identity, file effects, approved checks, model admission, evaluator interchange, stages, and recovery. Its completion inventory measures code and offline conformance. Live provider behavior and a particular deployed Docker boundary are separate verification results.

[How to use](how-to-use.md) contains commands. [Contracts](contracts.md) maps the versioned formats. Unsupported capabilities refuse admission; they are never invented by a model or a configuration flag.

## Authority and supported effects

The implementer receives text and proposes AgentStep 0.2. It receives no signing keys, database credentials, provider credential values, Docker socket, or executable tool channel from a native driver. The controller interprets neither arbitrary shell text nor provider tool calls. Model-selected delegation/release operations are outside this engine.

The typed broker registry admits `workspace.read`, `workspace.apply`, `check.run`, and `evaluator.import`. The context builder reads snapshots; file effects, checks, and evaluator imports use the corresponding authorizations internally. This is a small internal registry, not a general public RPC execution service. Unknown tools/actors/arguments and model-supplied authority fields are rejected. Task/candidate digests and a current positive lease fence are required. The controller adds action ID, policy digest, actor, lease, and `repeatable`/`reconcile` classification. Typed results record actual effects, artifact hashes, usage, and outcome.

Read/check effects are repeatable under the approved contract. File journals reconcile old/new byte identities before continuing. Container/model dispatch and evaluator imports persist intent before effects. Missing results preserve uncertainty. Push, merge, deployment, payment-like operations, schedulers, arbitrary tool plugins, and parallel agents need separate integrations and authorization.

## Lifecycle and deadlines

`NEW → PREPARING → PLANNING → EXECUTING → VERIFYING → CHECKPOINTING` is the basic path. Each checkpoint commits a pending next state before entering it. Required missing review leads to `REVIEWING`; missing other evaluators lead to `BLOCKED`. Current failures lead to diagnosis/replanning or a stall breaker. Other outcomes are `AWAITING_INPUT`, `PAUSED`, `BUDGET_EXHAUSTED`, `CANCELLED`, `FAILED`, and `SUCCEEDED`.

Pause/cancel are independently persisted signals and can arrive while a writer runs a bounded child. Cancellation takes precedence. Resume clears an explicit pause, reconciles effects, and requires current verification. It preserves attempts, stall/repeat counters, cumulative active time, known usage, and unknown holds. Succeeded/cancelled/failed/budget-exhausted revisions do not silently resume implementation.

An amendment is an explicit operator command with a note, same task ID, exactly the next revision, and unchanged accounting currency. Pause/stop and reconcile outstanding work first. A task amendment cannot alter the frozen project profile or engine configuration. It preserves cumulative counters and reservation rows, records old contract identity, and invalidates earlier acceptance/evaluation requests. Cancelled/failed runs require a new run. Runtime/evaluator registration changes require a new run.

Native top-level operations meter preparation, context, inference, edits, verification, imports, and checkpoints once. Nested timing does not double-charge. Normal idle waiting is excluded. Interrupted operations conservatively charge the observed wall-clock gap. Process deadlines use remaining active time; implementation leaves `max(configured reserve, sum of command timeouts)` for checks. Docker control/cleanup has bounded additional grace, and measured overhead is charged. These are controller/process limits, not a machine-wide scheduler or an instant deadline for every filesystem syscall.

An admitted final iteration may still apply and verify its proposal. Admission to another inference checks attempt/time limits again. Read-only finalization cannot waive missing evidence or authorize another model call.

## Runtime boundary

`local` executes approved checks under the controller's OS user and retains operator supervision. It cannot establish a separate evidence principal or unattended containment. POSIX owns/kills a process group; Windows local execution does not advertise equivalent group containment.

`docker` requires a preinstalled immutable image, Linux containers, seccomp availability, and successful deterministic probes. It never pulls images. Containers receive a fresh read-only workspace only, a nonroot UID/GID, read-only root, private IPC, no external network, all capabilities dropped, no-new-privileges, memory/swap/CPU/PID limits, and bounded temporary storage. Host credentials/state/socket are not mounted. The image is operator-approved and must include Python 3 plus the check environment. Check-generated outputs belong in `/tmp`.

Before checks execute, the engine inspects actual owner, image identity, mounts, user, namespaces, privileges, network and resource settings. Probes exercise root write denial, host-file invisibility, nonroot identity, network denial, timeout and removal. Failure leaves capabilities unavailable. Container names and owner labels are persisted before creation; cleanup removes only the matching owner and confirms absence. An inaccessible daemon or another owner remains an unknown effect even after an operator note.

The host, daemon, kernel and image are trusted deployment dependencies. Host administration can access the ledger/keys. The current offline suite verifies policy construction and refusal/reconciliation fixtures; it does not certify the current Docker host. See Docker's [run controls](https://docs.docker.com/engine/containers/run/), [none network](https://docs.docker.com/engine/network/drivers/none/), and [security model](https://docs.docker.com/engine/security/).

## Spend admission and uncertainty

A reservation identifies a frozen request digest, input count, output maximum, pricing identity/currency, verified-bound flag, status, actual usage, and any bound violation. It is saved in the authenticated projection while holding the writer lock **before inference dispatch**; iteration is incremented at the same admission. No response is sent when admission fails.

For a hard cap, held/unknown requests consume their full conservative ceiling; settled requests consume observed usage. Add final reserved token/cost capacity before admission. Reject unsupported input bounds, missing cost pricing, currency mixing, duplicate request IDs, and portable-integer overflow. Rates are operator-frozen conservative ceilings; cached input receives the full input rate. The ledger covers managed text inference, not external accounts/services or all possible provider billing products.

The OpenAI driver calls the Responses input-token endpoint with the frozen input and text schema and sets a maximum output. Its declared protocol supports a conservative bound. [OpenAI's token-counting guide](https://developers.openai.com/api/docs/guides/token-counting) describes counting Responses inputs. Anthropic's endpoint supplies [estimated counts](https://platform.claude.com/docs/en/build-with-claude/token-counting); hard spend admission is refused for that driver. Actual live conformance remains unverified.

Missing/malformed/ambiguous usage becomes `unknown` and keeps its hold. A crash after admission never means a free or unexecuted request. Restart/retry/model switching does not release or reset it. Conflicting duplicate settlement is rejected. A reported bound violation blocks additional implementation. Out-of-range actual totals preserve unknown usage and a violation instead of corrupting canonical state. Without a conservative bound, upper totals are `null`; known subtotals are reported separately. Unmanaged manual proposals cannot certify native hard caps.

Final reserves are admission headroom; this engine does not automatically call a paid reviewer. A later paid evaluator integration must reserve and reconcile its own charges before claiming a combined cap. Provider tariffs/model limits must be reviewed for the chosen deployment.

## Provider boundary and context

OpenAI Responses and Anthropic Messages drivers share `prepare`, `quote`, `respond`, `usage`, `step`, `events`, and `describe`. `events` normalizes a completed bounded response into typed events; this release does not stream provider responses. The HTTP-only worker has fixed TLS API endpoints, no redirects/proxy forwarding/autoretries, explicit credential environment references, byte/time bounds, sanitized errors, and owned cancellation. Remote inference may continue after local cancellation, so its reservation remains held/unknown.

Malformed JSON, incomplete responses, refusal, non-text/tool output, bad nested shapes, and oversize responses cannot reach effects. Native invalid/transient responses get at most one additional attempt across invocations; auth/permission failures wait. Each admitted inference consumes another attempt/reservation. Generic CLI failure classification remains heuristic.

ContextBundle 1.0 embeds the portable 0.2 bundle plus current implementer context ID, required instruction bytes/hashes, stage, ledger balance, runtime proof, and provenance-bound pending review findings. Accepted user/task/runtime authority takes precedence; repository conventions apply within that scope; logs and source cannot grant new permissions. Root/nested `AGENTS.md`, `LOOP.md`, and `.loop/agent-instructions.md` are mandatory even when excluded from ordinary source snapshots. Changes during dispatch invalidate the proposal.

Optional source/history can be trimmed. Task/state, required instructions, stage, reservations and pending findings are never silently truncated. Failure to fit required context refuses dispatch. Findings from an earlier candidate remain labelled `current: false`. Model switching creates a fresh context, checks required driver capabilities, preserves task/counters/accounting, and refuses an outstanding dispatch. The same model in a separate evaluator context can provide review; identity/context separation cannot prove good judgment.

## Evaluator requests and evidence

The controller signs an immutable procedure request containing task, contract, candidate, environment, check definition and fresh evaluator context. A registered role/check key signs a result after the procedure is performed. Types `review`, `human`, `interaction` and `artifact` share this interchange; the evaluator, not the engine, supplies actual domain observations.

Import verifies key availability/role/check registration, MAC purpose, unconsumed request, all bindings, separate context, findings, and real bounded regular-file artifacts. Passing results cannot carry blocking findings. Artifact bytes are copied into the private collector store and their original signed attribution is preserved in the authenticated projection. Replayed/stale/tampered results and revoked keys fail. Required unavailable services cannot be waived.

Controller/collector/evaluator envelopes use role- and purpose-separated HMAC-SHA256 keys stored outside the workspace with private POSIX permissions. HMAC verification requires trusted host key access; it is not public-key federation. A protected implementer cannot reach those authorities through the native text driver or check mount. Local arbitrary subprocesses share host authority, so local mode stays supervised. Signing establishes the registered origin, not the truth of a claim.

The deterministic gate receives only verified record/independence digests. Every required check must pass for the exact contract/check/candidate/environment, artifacts must still hash correctly, and outstanding effects/policy/bound violations block success. The controller rechecks the candidate before its successful checkpoint.

## Stages and persistence

Stages are a sequential DAG. Every criterion has one owner; dependencies are known/acyclic; each stage has scope restrictions subordinate to task scope. Ready stages are selected deterministically. Passing baseline criteria may satisfy stages. A stage pass is tied to one candidate and dependencies; changes derive all stage status again. Final integrated checks, including global evaluators, remain required.

Each SQLite transaction commits the event, state projection, and controller-signed versioned replay checkpoint together. The checkpoint authenticates a complete projection and predecessor-envelope digest. Event identity is `(run_id, sequence)`; sequence and leases increase monotonically. Writer locks coordinate runs and the same user's physical workspace across stores/aliases. Distributed hosts or other OS owners need an external lease service.

`get` reads one consistent projection/checkpoint pair and verifies identity/signature. `audit` verifies the whole chain, event/projection equality, sequences, leases and run identity; it deterministically reconstructs the last projection. This is a **versioned projection checkpoint stream**, not an event-payload-only reducer. `rebuild` holds the writer lock and advances the lease fence; it preserves pending effects and separate cancellation/pause signals. Broken chain/signatures are not repaired by inventing history. Legacy unsigned databases require explicit reviewed migration/new tasks; portable task 0.1 compatibility remains supported.

## Rule-to-code and offline coverage

| Rule | Implementation | Offline evidence |
| --- | --- | --- |
| Contract/scope/strict proposal | `contracts.py`, `workspace.py`, `controller.py` | `test_core.py`, `test_engine.py` |
| All-check current-snapshot gate | `reference/core.py`, `controller.py`, `native_engine.py` | failing/stale/tampered/oracle cases |
| Owned deadline/cancel/pause | `processes.py`, `runtime.py`, `native_engine.py` | real child cancellation, concurrent pause, runtime refusal fixtures |
| Broker role/lease/policy | `broker.py`, native effect dispatch | wrong actor/tool/argv/authority fields |
| Evidence/key/context identity | `authority.py`, `evaluators.py`, native gate | forged/revoked/replayed/stale review, artifact tampering |
| Conservative managed spend | `budgets.py`, `models.py`, native admission | no-dispatch boundaries, unknown holds, usage overflow, real process crash |
| Provider-neutral context/switch | `models.py`, `native_contracts.py`, context builder | both synthetic protocols, required instructions/findings, capability refusal |
| Revisions/stages/integration | `native_engine.py`, `stages.py` | cumulative amendments, DAG invalidation, integrated acceptance |
| Atomic durability/reconstruction | `store.py`, `workspace.py` | failed acceptance transaction, process/file crash, cross-store lock, replay/projection tampering |

The complete reviewed inventory is [implementation-status.json](implementation-status.json). [The native demo](../scripts/demo_native.py) assembles these paths without a network/model call. Live performance, representative-task results and deployment containment remain separate evidence.
