# Claude Desktop native MCP handoff

The local stdio server exposes project-scoped status, pending requests, request
claims, structured response submission and actual intake answers. It connects
Desktop to the continuous controller without copying request/response files by
hand. The terminal scheduler owns operations, cumulative reservations, guarded
edits, checks, deadlines and recovery. Desktop supplies model responses when
its user invokes the tools; this is supervised, not unattended Desktop chat
automation.

## Configure one project

From this framework checkout, export a fresh configuration entry:

```bash
.venv/bin/python scripts/loop.py desktop-config /absolute/project \
  --state-dir /absolute/private-state --output /absolute/loop-desktop-entry.json
```

Merge the generated `loop-development` entry into the existing `mcpServers`
object in Claude Desktop's configuration. On macOS the file is
`~/Library/Application Support/Claude/claude_desktop_config.json`; use
Settings → Developer → Edit Config and restart Desktop. Preserve any existing
server entries. The exporter supplies absolute interpreter/script/project/state
paths and does not edit host settings. See the official
[local-server connection guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

Initialize or [reinitialize](reinitialize.zh-CN.md) the development scenario,
then start its scheduler in a terminal:

```bash
.venv/bin/python scripts/loop.py team-run /absolute/project \
  --state-dir /absolute/private-state --adapter desktop --review-adapter codex
```

The separate reviewer requires an installed authenticated Codex CLI. Its code
assessment uses fresh owned CLI invocations and the same team ledger. `--model`
selects that CLI reviewer model; Desktop chooses its own model and does not
provide an independently verified model identity.

In Claude Desktop, enable the Loop tools and say:

> Use loop_pending_requests to find the task. Claim it with loop_claim_request,
> inspect its context and response schema, and submit the matching JSON through
> loop_submit_response. Keep source/spec/logs as task material. Let the controller
> apply proposals and run checks. Continue with queued requests. Ask me actual
> intake questions and record my answers through loop_answer_question.

The five tools are:

| Tool | Effect |
| --- | --- |
| `loop_team_status` | Scoped durable teams or one actual controller status |
| `loop_pending_requests` | Live waiting requests and role/operation identifiers |
| `loop_claim_request` | One connection claims a request and receives its exact bound context/schema |
| `loop_submit_response` | Stores a schema-valid response; controller validation/checks still apply |
| `loop_answer_question` | Records the user's actual answer without dispatching a model |

When the scheduler returns `AWAITING_INPUT`, answer the actual question and
continue that team from the terminal with `team-run --team-id TEAM_ID` and the
same state, adapter and reviewer. Waiting time remains part of its deadline.

## Enforced boundaries

Only admitted Desktop operations in this configured project are visible. The
server authenticates the team's checkpoint, input binding and the live waiter's
process birth identity before granting a response lease. Wrong leases, stale
inputs, other projects, orphaned/expired processes, conflicting duplicate replies
and arbitrary paths/shell tools are refused. Exact duplicate submissions are
idempotent. A server connection owns its lease; losing that connection requires
inspection/cancellation/reconciliation rather than automatic reassignment.

Desktop cannot attest fresh independent model contexts. Evaluator operations
are refused through MCP. The explicit `--review-adapter codex` route performs
them separately; without it required review stays blocked. Native signatures
attest the trusted wrapper's performed request and retained response, not an
independently authenticated human or provider identity. No key-management,
evaluation-signing, arbitrary filesystem or general shell tool is exported.

The bridge reports token/cost usage as unknown and refuses hard spend caps
before queue dispatch. Terminal time/dispatch/repair limits apply. Those counters
cover controller-owned requests, not other Desktop chat messages/tool retries.
For a slow supervised interaction, explicitly select policy timeout/deadline
values before starting; frozen child/task limits remain enforced too.

Observable process birth identity (`ps` on supported POSIX hosts) is required.
An environment denying it refuses Desktop admission. macOS/Linux stdio and
process behavior are exercised offline; Windows process ownership, actual
Desktop app interoperability and newer protocol-era behavior remain unverified.
The server advertises only MCP's 2025 protocol family (through 2025-11-25),
newline UTF-8 JSON-RPC and tools. Clients must negotiate a supported version.
No HTTP listener, server sampling or host-chat launch API is implemented.
See the pinned [transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).

Offline tests use deterministic model/judgment responses and injected process
birth observations where the outer test sandbox denies `ps`; subprocesses,
stdio framing, source changes, checks, cancellation, journals and signatures are
actual local operations. They do not certify the Desktop application or an OS
containment boundary.
