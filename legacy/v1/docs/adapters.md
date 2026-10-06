# Model and agent adapters

An adapter translates a backend into the portable task/proposal contract. Its declaration does not establish permissions, runtime isolation, or good coding performance. [How to use](how-to-use.md) provides operator commands; [native rules](native-engine.md) specify dispatch and uncertainty.

## Implemented routes

| Route | Input/output | Who executes effects |
| --- | --- | --- |
| File bridge | Fresh context JSON → strict AgentStep 0.2 | Controller applies guarded edits/checks |
| Generic command | File-backed context stdin → bounded final AgentStep stdout | Supervised controller; wrapper must only propose |
| Codex CLI proposal driver | Ephemeral schema-constrained invocation and JSONL usage/events | Supervised controller; agent asked to propose only |
| Desktop native MCP | Scoped queued context/schema → leased structured response; actual question answers | Continuous supervised controller; separate Codex evaluator |
| OpenAI native | Responses request/schema and input count → text AgentStep/usage | Native broker and configured check runtime |
| Anthropic native | Messages request and estimated count → strict text AgentStep/usage | Native broker and configured check runtime |

The workflow/file bridge is available to other capable text models and existing agents. Direct providers are deliberately limited to the two implemented drivers. Adding a provider does not require changing task, acceptance, stage or recovery rules. No model-selected tool channel is supplied by native drivers.

Generic/Codex `drive` applies to local compatibility runs. `drive-native` applies to native runs. Manual native proposals are allowed with unmanaged spend reporting when hard token/cost caps are absent. All live backend conformance remains unverified by the offline fixtures.

## Native interface

```text
describe() -> AdapterManifest
prepare(ContextBundle 1.0) -> frozen provider request
quote(request, timeout) -> input count, output maximum, pricing, bound flag
respond(request, timeout) -> bounded complete provider response
usage(response) -> normalized counters | unknown
step(response) -> strict AgentStep 0.2
events(response) -> typed usage/step/response_ended list
```

This release normalizes a final response into events; it does not implement live streaming or provider session resume. The ContextBundle preserves accepted task/state, source hashes, mandatory instructions, stage, reservations, runtime and review findings. Optional source/history may be trimmed; mandatory data cannot be.

OpenAI uses [Responses](https://developers.openai.com/api/reference/python/resources/responses/methods/create), [structured output](https://developers.openai.com/api/docs/guides/structured-outputs), and [input-token counting](https://developers.openai.com/api/docs/guides/token-counting). The output maximum includes provider output tokens; usage output is not increased again by reasoning-token details. Cached input is not double-counted.

Codex and OpenAI generation schemas supply explicit scalar types and singleton
enums for constants. Objects stay closed with all fields required. String length
and array uniqueness constraints are enforced against the original local
contract after parsing; they are omitted only from the transport schema.
Unsupported compositions or optional/open objects refuse preparation. The same
translation covers team coordinator, worker, review and receipt responses.

Anthropic uses [Messages](https://platform.claude.com/docs/en/api/messages/create) and [estimated token counts](https://platform.claude.com/docs/en/build-with-claude/token-counting). Native JSON parsing enforces the proposal shape without claiming provider-enforced structured output. Reported cache read/creation input is included. Estimated admission cannot certify a hard token/cost cap.

The HTTP worker talks only to the fixed official endpoints over verified TLS, without redirects, ambient proxy forwarding, SDK tools or automatic HTTP retries. It reads the explicitly named credential from the trusted host environment. Requests/responses/time are bounded; errors omit raw provider bodies. Owned local cancellation cannot roll back remote inference, so ambiguous spend retains its reservation.

Malformed/oversize/incomplete/non-text/tool output cannot reach edits. Invalid/transient proposals receive at most one additional attempt across invocations, under cumulative admission. Authentication/permission errors wait; missing required usage blocks. A model switch creates a fresh context and refuses dropped required capabilities or outstanding dispatch.

## Capability negotiation

[Adapter](../schemas/adapter.schema.json) and [runtime](../schemas/runtime.schema.json) capabilities are separate. Only Boolean `true` grants support; missing entries are unavailable. Native drivers support text, headless request transport, normalized events and reported usage; only the OpenAI driver declares provider-enforced structured output. Vision, model-selected tools, session resume and provider-side cancellation are not advertised.

The local controller records effective manual supervision because arbitrary subprocesses share host authority. The native Docker path admits unattended checks only after actual containment probes/control verification. Required missing runtime/model capabilities reject admission; a configuration flag cannot waive them. A budget protocol can be supported while a live deployment/provider conformance result is still unverified.

The generic wrapper declares only text/headless dispatch, with spend unknown. Codex also declares its schema/events/usage interface; absent or interrupted usage keeps total unknown and preserves known subtotals. CLI versions/flags and behavior require conformance on the operator's installed backend. [The local guide](local-controller.md#command-wrappers-and-codex-cli) records the inspected CLI version and supervised limits.

The [Desktop MCP bridge](desktop-mcp.md) implements the 2025 stdio tool protocol,
project scope, live-waiter/lease binding and budgeted controller operations. It
does not attest Desktop model identity, fresh chat contexts or spend. Required
independent review uses an explicit separate Codex host. Other optional
[ACP](https://agentclientprotocol.com/protocol/v1/initialization) or MCP routes
must preserve these semantics and verify capabilities. No connector is needed
for the file bridge.

## Extend and verify

Implement a new driver's request translation, conservative quote (or explicit unsupported bound), usage normalization, strict proposal parsing, sanitized errors, limits and capability manifest. Inject an offline transport first. Test refusals, malformed/oversize responses, tool output, missing usage, auth/rate-limit errors, ambiguity after dispatch and context/model switching. Persist reservation/attempt before inference and never release unknown spend merely because a request failed.

Later, explicitly test the actual provider/agent under bounded credentials/budgets and record model/driver/configuration versions, observed cancellation/usage, and task acceptance. [Evaluation](evaluation.md) separates those results from synthetic protocol conformance.
