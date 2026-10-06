# Evaluation and recorded evidence

The delivered implementation is evaluated offline with deterministic proposals/transport fixtures, actual local source/check processes, and fault injection. These results establish tested controller behavior within that scope. They do not measure model intelligence, certify every backend, or attest a deployed isolation boundary.

## Development and live policy

[Implementation status](implementation-status.md) tracks required definitions, integrated code and offline coverage. The read-only prerequisite command is `python3 scripts/check_readiness.py --for-live-evaluation`. A met inventory does not run a model. A current full offline validation and explicit operator dispatch remain required. Default CI invokes neither providers nor Docker probes.

Live evaluation follows the implementation inventory and full offline validation.
Its results belong in a separate report; deterministic tests do not establish
live semantic quality. Representative task evaluation remains a separate phase.

[The bounded Codex CLI team evaluation](team-live-evaluation.json) records actual
attempts, observed limitations, usage coverage and an unchanged external oracle.
Its outcome is separate from the passing offline inventory.

The small Python fix passed its unchanged project tests. Phase recipients
accepted the implementation, but the subsequent independent review returned
inconclusive because its packet lacked the original source comparison and the
predecessor's actual test results. The controller did not record full-team
success. The packet correction passes the recorded full offline suite; its
live follow-up has not run because automatic approval review requires explicit
authorization for the temporary example data export and added evaluation
budget. The report preserves every actual attempt and separates this proposed
follow-up from admitted operations.

[offline-validation.json](offline-validation.json) records the current full local
suite, schemas/templates, readiness, source digest and verification limits.

## Recorded offline integrations

| Report | Actual mechanics exercised | Synthetic inputs |
| --- | --- | --- |
| [Local evaluation](local-evaluation.json) | Failing regression, rejected compatibility break, fresh-controller resume, focused fix, restored snapshot | Scripted file proposals; model spend unknown |
| [Native evaluation](native-evaluation.json) | Persisted reservation/restart, context switch, actual command checks, staged acceptance, registered signed import, replay audit and restore | Both provider response protocols, token counts/prices and reviewer judgment |

Reproduce with `scripts/demo_local.py` and `scripts/demo_native.py`. Use `--keep` to retain actual journals/artifacts and `--report` for a new measured summary. The native report keeps an ambiguous request unknown while reporting known subtotals separately. Neither demo sends network/provider requests.

`test_core.py` checks deterministic gate/canonical/capability/budget decisions. `test_engine.py` executes real file/proposal/check/journal cases, including a process exit midway through edits and preservation of intervening user changes. `test_processes.py` checks bounded owned subprocess cleanup. `test_native.py` covers forged/revoked/replayed/stale evidence, same-context review, missing usage, hard-cap no-dispatch boundaries, provider shapes, mandatory instruction/finding context, frozen amendments, stages, projection tampering, cross-store locks, concurrent pause, and a real process exit after spend reservation.

Docker tests inject control/inspection fixtures to check policy and fail-closed behavior. They do not exercise the current daemon/kernel/image. Remote CI results are not implied by local execution; the workflow is supplied for repeatable offline checks.

`test_team.py` covers prepared/manual execution; `test_team_automation.py` adds
spec intake and actual answers, shared reservations across all operation types,
bounded repair, concurrent isolated workers, real integrated regressions,
separate signed review requests, cached-response crash recovery, capability
refusal before dispatch and actual process cancellation. Hosts and judgments
are deterministic fixtures; source changes, checks, integration and journals
are actual local operations.

`test_agent_roles.py` covers the shared specialist protocol, current covered
responsibilities, stale/pending coverage refusal, bounded exports, preserved
customization and legacy compatibility. Actual offline scheduling verifies that
workers, covering owners, covered recipients and separate signed branch/main
reviews receive their effective instructions. Protocol changes invalidate frozen
teams before dispatch. These cases exercise context and execution plumbing;
professional judgments and provider usage remain deterministic fixtures.

[The local collaboration corpus](offline-corpus.json) adds SQLite persistence,
an API/HTML data contract, actual injection/escaping checks, a measured 500-item
local workload, seven specialist responsibilities and separate signed assessment
requests. A second case injects an incompatible API proposal; unchanged real
tests detect it and feed the next proposal. These are deterministic host/judgment
fixtures and actual local checks, not deployed browser/load or model-quality
benchmarks. Reproduce with `scripts/demo_team_corpus.py --report REPORT.json`.

Desktop tests exercise actual MCP stdio/client/waiter processes, scope and lease
refusals, expiry/cancellation, unknown spend and separate reviewer import. The
outer macOS test sandbox denies `ps`, so its process-birth observation is injected;
actual Desktop UI interoperability remains a separate check.

## Later deployment and live conformance

Before enabling unattended work, explicitly verify the configured immutable image, actual check environment, identity/mount/network/privilege/resource controls, bounded cancellation and named-container reconciliation on that host. Required browser or external evaluator capabilities need their own observed service evidence. Unsupported capabilities remain blocked.

For a later live provider evaluation, fix a task/oracle/starting snapshot and bounded budgets; record request/driver/model/configuration identities, actual usage coverage, timeout/cancel behavior, malformed/auth/transient failures, restart and second-provider context switching. An ambiguous remote request stays charged conservatively. Do not weaken the oracle or reset resources to improve the result.

## Representative comparison

Use a fixed corpus covering reproducible bugs, features, compatibility refactors, actual UI interaction, meaningful ambiguity, interrupted recovery and maintenance. Keep independent checks outside implementer control. Hold model/task/budget constant while comparing workflow variants; then compare providers while reporting capability differences. Randomize order, preserve all attempts and report sample size/uncertainty.

| Metric | Record |
| --- | --- |
| Verified success / false completion | Independent oracle over every attempted/delivered task |
| Regression | New required failures relative to baseline |
| Cost / time per accepted task | All attempts, retries, checks, reviews and recovery; unknown spend coverage explicit |
| Intervention / recovery | Human involvement, lost work, duplicated or unresolved effects |
| Policy | Attempted and executed violations separately |
| Handoff | Fresh contexts continuing without prior chat or invented state |

Include blocked, stalled, cancelled and exhausted outcomes in the denominator. A single scripted success, model agreement, repository popularity or more agents does not prove a better framework. Set deployment thresholds for the actual use case after observed results.
