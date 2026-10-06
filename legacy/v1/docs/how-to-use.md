# How to use Loop Engineering

Use this project in two ways: add its portable workflow to an existing coding agent, or run the native controller that owns proposals, checks, budgets, review, and recovery. A text-capable model can participate through the JSON file bridge. Direct provider drivers currently cover OpenAI Responses and Anthropic Messages; other models can use the file bridge or a custom driver.

Implementation and offline checks precede live evaluation. Commands marked **future live dispatch** require a separately chosen evaluation; setup, context export, scripted demos and local verification invoke no model.

For a development project, the [development scenario](scenarios.md) provides a
spec-first entrypoint. For App/CLI conversation coordination, make a one-time
project connection with `host-install PROJECT --state-dir PRIVATE_STATE --host codex`,
reconnect the project MCP tools and invoke `$loop-engineering`. The current host
operates Loop, collects actual answers and shows controller progress in a local
page. [对话式接入指南](native-host.zh-CN.md) covers Codex, Claude Code and Desktop,
the existing-team boundary and required evaluator/unknown-spend limits.

The separately scheduled command-line route is also supported:

An idea or existing docs can first go through the [setup requirements agent](requirements-setup.zh-CN.md):
`setup PROJECT --state-dir PRIVATE_STATE --adapter codex`. It prepares a spec and
stops; continue with `team-run --team-id` to preserve the same budget and answers.

```bash
.venv/bin/python scripts/loop.py init /absolute/path/to/project \
  --scenario development --spec /absolute/path/to/spec.md
.venv/bin/python scripts/loop.py team-run /absolute/path/to/project \
  --state-dir /absolute/private-state --adapter codex
```

`team-run` uses the selected installed host for continuous supervised work;
see [team execution](team-execution.md) for questions, repair, parallel isolation,
budgets and continuation. Alternatively, ask a coding host to read `.loop/start.md`.
Its
coordinator asks material questions, prepares the task/profile, assigns the
relevant specialists and manages integration and verification. The user does
not need to perform the manual task editing in section 2. Host delegation is
capability-dependent. Claude Desktop supports the [native MCP handoff](desktop-mcp.md)
with a separate Codex reviewer, as well as the portable file workflow.
For an existing setup, use the [重新初始化指南](reinitialize.zh-CN.md).
The remaining guide also documents the underlying manual/controller routes.

## 1. Install and try an offline example

Use Python 3.11 or newer. In this checkout:

```bash
cd /Users/super/Documents/ai/loop-engineering-code
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/demo_local.py
.venv/bin/python scripts/demo_native.py
```

The first demo rejects an overbroad fix and accepts a focused one using actual Python checks. The second exercises two synthetic provider protocols, an ambiguous spend reservation, controller restart, model switching, staged checks, authenticated review import, replay, and restoration. Their proposals, provider usage, prices, and reviewer judgment are fixtures. Neither is a model benchmark.

Add `--keep /absolute/path/to/a-new-directory` to either demo to retain its project, state, logs, and delivered snapshot. Add `--report /absolute/path/to/report.json` to save the measured summary. The output directory must be fresh.

Define these variables for the remaining examples; replace the target paths:

```bash
LOOP_HOME="/Users/super/Documents/ai/loop-engineering-code"
LOOP_PY="$LOOP_HOME/.venv/bin/python"
LOOP_CLI="$LOOP_HOME/scripts/loop.py"
PROJECT_DIR="/absolute/path/to/your-project"
LOOP_STATE_DIR="/absolute/path/outside-your-project/loop-state"
```

The state directory must be outside the target workspace. It contains task identities, private keys, the SQLite journal, artifacts, and recoverable file contents. Keep it under the controller/operator's ownership; do not give a coding agent access to it.

## 2. Prepare your project and acceptance contract

```bash
"$LOOP_PY" "$LOOP_CLI" init "$PROJECT_DIR"
```

`init` creates missing `LOOP.md`, `.loop/project.json`, `.loop/task.json`, `.loop/handoff.md`, and `.loop/agent-instructions.md`. Existing files and agent instructions are preserved. It discovers candidate commands without running them. Merge the pointer in `.loop/agent-instructions.md` into the instruction file your agent already reads, or paste it into the session.

Edit `.loop/task.json` before starting:

| Field | Set it to |
| --- | --- |
| `objective`, `non_goals`, `compatibility_requirements` | Observable behavior and explicit boundaries |
| `scope.write_allow` | The narrowest useful set of files or patterns |
| `scope.write_deny` | Approved tests, fixtures, sensitive files, and other protected paths |
| `authorization.allowed_actions` | Effects you authorize; reading and required checks must be present |
| `criteria` | Stable IDs, behavioral descriptions, and existing `check_ids` |
| `checks` | Exact approved argv arrays or an identified external procedure |
| `limits` | Attempt, active time, stall, repeat, and optional spend limits |
| `required_capabilities` | Capabilities the task cannot proceed without |

Remove `extensions.scaffold` after replacing the generated placeholder task. All declared checks are required, including global checks not attached to a criterion. A successful build alone may not exercise the behavior you want. Protect the acceptance oracle from proposal edits.

Review `.loop/project.json`: command candidates, conventions, source priorities, context byte limit, and snapshot exclusions. Execution uses the task's approved checks. The profile's `confirmed` flags record your review of discovered commands. Dependencies omitted from snapshots must be provisioned in the check environment; commands do not install them automatically.

Scope patterns use Python `fnmatchcase`, where `*` can match `/`; deny rules win. Paths are normalized and checked against traversal, symlinks, and spelling aliases. `.git` and `.loop` are always protected. AgentStep currently supports UTF-8 file replacement/creation, with guarded byte hashes. Binary edits, deletion, mode changes, and submodule provisioning require another integration.

```bash
"$LOOP_PY" "$LOOP_CLI" doctor "$PROJECT_DIR"
```

Static `doctor` executes no project commands or provider calls. Resolve its errors before starting. Warnings describe actual capability limits.

## 3. Use the portable workflow with an existing agent

Give the agent `LOOP.md`, the accepted task, existing repository instructions, and [the start prompt](../prompts/start.md). Maintain `.loop/handoff.md` with actual observations, failed approaches, current checks, and one next action. Use [the resume prompt](../prompts/resume.md) with a fresh model/session.

The workflow can also be used without the CLI: a human or the agent's host applies changes and verifies the current code. Identify that verifier in the handoff. Prompt instructions alone do not enforce process permissions or evidence integrity.

For enforced file proposals, start a local run:

```bash
"$LOOP_PY" "$LOOP_CLI" start "$PROJECT_DIR" --state-dir "$LOOP_STATE_DIR"
```

Copy the returned run ID into `RUN_ID`:

```bash
RUN_ID="run-replace-with-returned-id"
"$LOOP_PY" "$LOOP_CLI" context "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --output /absolute/path/to/context-1.json
```

`start` freezes the task/profile, captures source inputs, and runs baseline checks. A failing baseline is normal for a bug. Already satisfied command-only tasks can finish without an implementer. `--no-baseline` skips baseline checks deliberately. It does not waive final checks.

Supply the fresh context and [AgentStep 0.2 schema](../schemas/step-v0.2.schema.json) to any capable model. Ask for one complete JSON object without Markdown fences. Use the supplied contract digest, base snapshot digest, and existing file hashes. Choose a fresh `step_id`; identify the criteria and expected observation; supply full changed file contents. New files use `expected_sha256: null`. There is no `done` intent. `request_verification` asks the controller to check the candidate.

Save the proposal outside the input tree and submit it:

```bash
"$LOOP_PY" "$LOOP_CLI" step "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --file /absolute/path/to/step-1.json
"$LOOP_PY" "$LOOP_CLI" status "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --events
```

Accepted proposals are applied through file guards, then every required command runs on a fresh materialization. Stale hashes, wrong scope, forged evidence references, modified or missing checks, and unknown fields are rejected. Export a new context after any workspace change. Context/request/result output files are created exclusively; choose a new filename instead of overwriting an earlier export.

A generic wrapper or Codex CLI can automate this file exchange in a later live evaluation. Those routes remain supervised and have no hard future-spend bound. See [local adapters](local-controller.md#command-wrappers-and-codex-cli). Native runs use `drive-native` or manual proposals.

## 4. Configure the native engine

```bash
"$LOOP_PY" "$LOOP_CLI" init "$PROJECT_DIR" --native
```

This additionally creates `.loop/engine.json`. A subsequent `start` detects that file automatically; `--engine /absolute/path/to/engine.json` selects one explicitly. Existing local runs retain their original controller.

Review [the engine template](../templates/engine.json):

- `runtime`: choose `local` for supervised checks; choose `docker` only with a preinstalled immutable image and available containment controls.
- `model`: explicit provider, actual supported model ID, environment-variable name for the credential, and optional frozen pricing.
- `response_limits`: request/response byte bounds, output-token bound, per-request timeout, and reserved final token/cost capacity.
- `evaluator_keys`: registered evaluator key IDs, roles, and permitted non-command checks.
- `stages`: an optional sequential dependency graph, required when the task uses `workflow: staged`.

Replace `set-your-model-id` before any live dispatch. The model configuration stores an environment-variable name, never a credential value. Supply credentials only to the trusted controller environment using your usual credential mechanism. They are not put into the proposal context or check container.

For pricing, specify `price_id`, the task's currency, and conservative `input_microunits_per_million`/`output_microunits_per_million`. One currency microunit is one millionth of that currency. Consult the provider's current tariff for your actual model/service tier; the demos' prices are fictional. Cached input is conservatively charged at the full configured input rate. Fixed surcharges, external paid evaluators, tool billing, and other accounts need separate accounting; this ledger covers controller-managed text inference.

Hard token/cost admission requires a conservative input/output bound. The OpenAI driver counts the frozen request and reserves its output maximum. The Anthropic count endpoint documents estimates, so that driver refuses hard token/cost caps. With nullable caps it can still report known usage and unknown coverage. The file bridge marks model spend unmanaged; it cannot bypass native hard caps.

The controller saves a reservation before inference, settles reported usage afterward, and keeps the full hold when a response is ambiguous. Restart, retry, or model switching cannot reset that hold. Missing usage stays unknown. If a provider exceeds its admitted bound, further implementation waits for accounting reconciliation. Increasing an explicitly amended budget retains earlier usage and unknown holds.

### Optional protected check runtime

Set `runtime.backend` to `docker` and `runtime.image` to an image ID such as `sha256:<64 hex digits>` or a repository digest such as `your-image@sha256:<64 hex digits>`. The image must already be installed, include Python 3 for the probes, and provide your approved check tools/dependencies. Use executable names/paths inside that image, not a host virtualenv path.

```bash
"$LOOP_PY" "$LOOP_CLI" doctor "$PROJECT_DIR" --probe-runtime
```

This explicitly runs deterministic containment probes, never a model. The backend uses a nonroot user, read-only root and workspace, no external network, no added capabilities, no-new-privileges, memory/CPU/PID limits, bounded `/tmp`, and named-container cleanup. Build/cache outputs must use `/tmp`. No image is pulled automatically. The actual container configuration is inspected before approved checks execute.

Unavailable daemon access, mutable/uninstalled images, failed probes, or missing required capabilities refuse protected operation. Local mode remains operator-supervised, and an unattended request requires verified Docker controls. Docker deployment behavior has not been verified in the current development session. The trusted host, daemon, image, and operator remain part of the authority boundary.

### Future live dispatch

After a current full offline validation and an explicit decision to run a provider:

```bash
"$LOOP_PY" "$LOOP_HOME/scripts/check_readiness.py" --for-live-evaluation
"$LOOP_PY" "$LOOP_CLI" drive-native "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --turns 1
```

Only `drive-native` starts direct inference. It supports OpenAI Responses and Anthropic Messages, with injected offline transports available to developers. Direct drivers request one text proposal and supply no model-selected tools. A local native run can complete with operator supervision; a passing readiness inventory does not certify the live backend or deployed isolation.

Native ContextBundle 1.0 contains task/source/state under `bundle`, plus `repository_instructions`, `active_stage`, `budget`, and `runtime`. Existing file-bridge ContextBundle 0.2 exposes task/source/state at the top level. AgentStep is 0.2 in both cases. Root/nested `AGENTS.md`, `LOOP.md`, and `.loop/agent-instructions.md` are mandatory native inputs even when excluded from source snapshots; required instructions are never trimmed to fit context.

## 5. Add required review or other evaluator checks

Add a `review`, `human`, `interaction`, or `artifact` check with a concrete `procedure`. A review may require `independent: true`. Register a key before starting the native run:

```bash
"$LOOP_PY" "$LOOP_CLI" key-create --state-dir "$LOOP_STATE_DIR" --key-id project-reviewer --role reviewer
```

Add `{"key_id":"project-reviewer","role":"reviewer","check_ids":["review"]}` to `engine.json.evaluator_keys`, using the actual check ID. Other check types require the matching `human`, `interaction`, or `artifact` role. Configuration is frozen at start; changing runtime/evaluator registrations requires a new run.

When the candidate is ready, export the procedure request:

```bash
"$LOOP_PY" "$LOOP_CLI" evaluation-request "$RUN_ID" --state-dir "$LOOP_STATE_DIR"   --check-id review --output /absolute/path/to/review-request.json
```

The request names the exact contract, check, candidate, environment, and separate evaluator context. Restore/export the candidate for the evaluator and confirm its snapshot digest; perform the stated procedure and retain real artifacts. An interaction evaluator is responsible for actually operating the app/browser. The engine supplies interchange and validation; it does not invent a browser run or human judgment.

From the trusted evaluator/operator environment, attest the performed procedure:

```bash
"$LOOP_PY" "$LOOP_CLI" evaluation-sign --state-dir "$LOOP_STATE_DIR"   --request /absolute/path/to/review-request.json --key-id project-reviewer   --result pass --summary "Describe the observed behavior and compatibility review"   --artifacts /absolute/path/to/review-report.txt --output /absolute/path/to/review-result.json
"$LOOP_PY" "$LOOP_CLI" evaluation-import "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --file /absolute/path/to/review-result.json
"$LOOP_PY" "$LOOP_CLI" resume "$RUN_ID" --state-dir "$LOOP_STATE_DIR"
"$LOOP_PY" "$LOOP_CLI" verify "$RUN_ID" --state-dir "$LOOP_STATE_DIR"
```

For findings, supply `--findings /absolute/path/to/findings.json` containing an array of `{id,severity,description,criterion_id}`; severity is `blocking` or `note`, and `criterion_id` is a valid ID or `null`. A passing result cannot contain a blocking finding. Use `fail` or `inconclusive` when appropriate.

The controller verifies the registered role, fresh unconsumed request, MAC, separate context, digests, findings, and artifact bytes; it copies artifacts into its private store. A changed candidate or instruction/environment invalidates the result. Review remains `REVIEWING` until evidence arrives; other missing services leave the task `BLOCKED`. The model cannot sign/import its own authority. Host administrators still control the keys; a signature proves origin, not the quality of the judgment.

Use `key-revoke --state-dir "$LOOP_STATE_DIR" --key-id project-reviewer` to revoke that evaluator. Existing evidence from a revoked key cannot certify a later verification. Create/register a replacement in a new run; built-in journal/collector keys require an audited migration.

## 6. Use staged work

Set the task's `workflow` to `staged` and put a graph in `engine.json.stages`. For the bundled slug task:

```json
[
  {"id":"fix","depends_on":[],"criterion_ids":["empty-whitespace"],"write_allow":["slug.py"],"write_deny":[]},
  {"id":"compatibility","depends_on":["fix"],"criterion_ids":["preserve-behavior"],"write_allow":["slug.py"],"write_deny":[]}
]
```

Every criterion has exactly one owner; IDs/dependencies must be valid and acyclic. A step targets the active stage's criteria/files as well as the task's scope. Shared files are allowed across sequential stages. Passing the baseline may already satisfy a stage; one focused fix may satisfy several. Stage passes belong to the exact candidate and are derived again after changes. Final success requires all task checks on the integrated candidate. No parallel agents are started.

## 7. Pause, amend, switch, recover, and deliver

```bash
"$LOOP_PY" "$LOOP_CLI" pause "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --reason "Operator reviewing the next change"
"$LOOP_PY" "$LOOP_CLI" resume "$RUN_ID" --state-dir "$LOOP_STATE_DIR"
"$LOOP_PY" "$LOOP_CLI" cancel "$RUN_ID" --state-dir "$LOOP_STATE_DIR"
"$LOOP_PY" "$LOOP_CLI" runs --state-dir "$LOOP_STATE_DIR"
"$LOOP_PY" "$LOOP_CLI" audit "$RUN_ID" --state-dir "$LOOP_STATE_DIR"
"$LOOP_PY" "$LOOP_CLI" restore "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --target /absolute/path/to/a-fresh-delivery
```

Pause is a recoverable signal polled during owned work. Cancellation wins over pause. Resume inspects pending actions and preserves cumulative counters. Restore creates a fresh directory from content-addressed blobs and never overwrites the workspace. `status` compares the working tree with its checkpoint; delivery is the verified checkpoint, not a later changed tree.

For an authorized native amendment, pause/reconcile first; use the same task ID and exactly the next revision, retain the billing currency, and save the new contract to a separate file:

```bash
"$LOOP_PY" "$LOOP_CLI" amend "$RUN_ID" --state-dir "$LOOP_STATE_DIR"   --task /absolute/path/to/revised-task.json --note "Record the approved change to behavior, scope, or budget"
```

Amendments invalidate prior acceptance evidence while preserving attempts, elapsed work, pricing identities, usage, and unknown reservations. Succeeded and budget-exhausted native runs can receive an explicit next revision; cancelled/failed runs need a new run. Do not edit the accepted task/profile/engine file behind the controller.

To switch a native model, save a model configuration object with `provider`, `model`, `api_key_env`, and `pricing`, then use `model-switch "$RUN_ID" --state-dir "$LOOP_STATE_DIR" --file /absolute/path/to/model.json`. The command rebuilds a fresh implementer context and retains task/budget identity. It cannot bypass a hard-cap accounting restriction or an outstanding dispatch.

After a crash, `resume` reconciles old/new guarded file contents, owned processes/containers, and held reservations. Intervening user edits stop file recovery before additional writes. A missing process result is an unknown effect. Inspect/stop the earlier action before `reconcile --action-id ... --note ...`; container cleanup additionally requires confirmed daemon ownership/removal. Never blindly replay an ambiguous action.

`audit` verifies the complete signed checkpoint chain and reconstructs state without modifying it. `rebuild` repairs a damaged projection only from valid replay and advances the lease fence; it preserves unresolved effects and independent cancel/pause signals. Broken signatures, sequence gaps, or missing blobs need operator restoration from a trusted backup. Old unsigned run databases are not automatically promoted; start a new task using explicitly reviewed code and accounting.

## 8. Validate changes to this framework

From the checkout:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_contracts.py
.venv/bin/python scripts/check_readiness.py --for-live-evaluation
.venv/bin/python scripts/demo_local.py
.venv/bin/python scripts/demo_native.py
```

These commands use deterministic transports and local check processes. Default CI runs the same offline paths. See [implementation status](implementation-status.md), [native rules](native-engine.md), and [evaluation](evaluation.md) for what completion means and what deployment/live validation remains unverified.

依赖准备、浏览器交互验证、HTTP 性能检查和新版文件操作的配置方法见[执行工具与重新设置指南](execution-tools.zh-CN.md)。
