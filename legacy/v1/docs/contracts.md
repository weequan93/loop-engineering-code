# Contracts and compatibility

JSON is the provider-neutral interchange; Markdown supplies readable workflow/handoff. Contracts use JSON Schema draft 2020-12 and strict integer-only canonical JSON. Public shapes reject undeclared fields. Explicit extension/payload objects carry controller/evaluator data without changing authority.

## Versions and ownership

| Contract | Version | Owner / schema |
| --- | --- | --- |
| Task | 0.2, with 0.1 compatibility | Authorized operator/controller; [task](../schemas/task-v0.2.schema.json), [legacy task](../schemas/task.schema.json) |
| Project profile | 0.2 | Frozen operator configuration; [profile](../schemas/project-v0.2.schema.json) |
| AgentStep | 0.2 default, opt-in 0.3 | Untrusted implementer proposal; [text step](../schemas/step-v0.2.schema.json), [file operations](../schemas/step-v0.3.schema.json) |
| Execution tools | 1.0 | Frozen operator configuration; [tools](../schemas/execution-tools.schema.json) |
| Adapter/runtime manifest | 0.1 | Driver declaration / verified host report; [adapter](../schemas/adapter.schema.json), [runtime](../schemas/runtime.schema.json) |
| State/evidence | 0.1 | Controller/collector; [state](../schemas/state.schema.json), [evidence](../schemas/evidence.schema.json) |
| Native engine/context/actions/evaluator requests/attestations | 1.0 | Controller or registered service; named definitions in [native bundle](../schemas/native.schema.json) |
| Controller event | 0.2 | Controller; native `controller_event` definition |
| Replay checkpoint | 1.0 | Signed controller projection stream; native `replay_checkpoint` definition |
| Implementation inventory | 0.1 | Maintainer reviewed against source/current offline validation; [readiness](../schemas/readiness.schema.json) |

Each format versions its own semantics; package 1.0.0 does not renumber established task/evidence contracts. The native schema is a **definition bundle**, not a root object schema. Call `validate_native(kind, value)` or select `#/$defs/<kind>` explicitly. `validate_context` additionally validates the embedded task/state and cross-identities. Saved templates/examples are checked by `scripts/validate_contracts.py`.

Task boundaries include non-goals, assumptions, dependencies, compatibility requirements, path scope, authorized actions, criterion/check links, capabilities and cumulative limits. Every listed check is required; global checks may be unreferenced by criteria. IDs/references and command/type consistency receive semantic validation beyond shapes. Generated scaffold tasks cannot start.

Costs use integer millionths of `cost_currency`; prices identify configured per-million input/output token ceilings. Nullable hard caps mean no hard cap, not zero spend. Unknown usage stays `null`, with known subtotals kept separately. Canonical values use UTF-8, Unicode-scalar key ordering, compact JSON, integers within ±9007199254740991, and no floats/duplicate keys/invalid Unicode. Use [shared digest vectors](../examples/digest-vectors.json) for another language.

## Proposal and action boundary

AgentStep has `act`, `request_verification`, `need_input` or `blocked` intent. It names the accepted task/contract and exact base snapshot, current file hashes, criteria, expected observation and bounded replacement contents. Nullable fields must be present. No success intent, arbitrary command, authority credential or self-authenticated evidence is accepted. Non-act steps cannot edit. Consumed step IDs cannot be replayed.

Native action shapes are executable:

| Definition | Fields supplied by owner |
| --- | --- |
| `action` | Proposal ID, task/contract/candidate identity, registered tool and arguments |
| `authorized_action` | Controller action/actor ID, current lease, policy decision, idempotency class and validated proposal |
| `action_result` | Actual outcome/effects, artifact digests and known usage |
| `response_limits` | Output/time/request/response bounds and final spend reserve |
| `reservation` | Request/price identity, bound, held/settled/unknown status, actual usage and violation |
| `context` | Portable bundle plus mandatory instructions, stage, budget, runtime and provenance-bound findings |
| `evaluator_request` | Immutable procedure and task/check/candidate/environment/context bindings |
| `finding` | Stable ID, blocking/note severity, description and optional criterion ID |
| `attestation` | Role/key/purpose, random nonce, portable payload and HMAC |

The broker registry validates tool-specific arguments for read/apply/check/import. An implementer cannot supply a policy decision or import evaluator evidence. `argv` is an exact approved argument array; no shell interpolation arises from model text. Path checks reject traversal, symlink access, protected controls and filesystem aliases. Scope uses documented Python `fnmatchcase`; deny wins.

## Durable state and events

SQLite commits event, projection and signed replay checkpoint together. The event envelope identifies run, sequence, timestamp, lease, type and payload. The signed checkpoint also has `schema_version: 1.0`, a complete projection and predecessor-envelope digest. The projection embeds separately validated state/task/profile; internal extension data is authenticated rather than interpreted as model authority.

Public event families cover task acceptance/revision, writer claims, operation timing, broker authorization/results, model errors/switches, reservations/usage, evidence/evaluation import, guarded action/process intent/results, recovery and checkpoint/state changes. Effect-bearing payloads use their named contracts; lifecycle diagnostics remain typed object payloads under the versioned checkpoint. Consumers identify `(run_id, sequence)` and reject conflicting/gapped replay. Actors are established by host keys and broker roles, not free-text assertions.

This implementation reconstructs state from an authenticated **projection checkpoint stream**. It does not claim an event-payload-only reducer, public-key federation, distributed leases or exactly-once remote execution. Crash-time missing results require reconciliation. Rebuild only accepts a valid complete chain, then advances the fence. Independent pause/cancel signals and unknown effects remain preserved.

Legacy unsigned databases have no trustworthy automatic migration. Start a new task from explicitly reviewed code/accounting; portable task 0.1 remains supported. Future incompatible replay versions must supply a reviewed migration, not reinterpret unknown authority fields.

[Native rules](native-engine.md) specify failure behavior and the rule-to-code/test map. [How to use](how-to-use.md) demonstrates configuration, proposals and signed evaluator interchange.
