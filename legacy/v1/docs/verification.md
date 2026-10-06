# Verification and evidence

Completion means that every required check in the authorized contract passes for the candidate being delivered. Evidence is trustworthy only when a collector outside the implementer's writable state authenticates its origin and artifacts.

The reference gate checks identities and externally attested records. It does not run checks, verify artifact storage, prove isolation, or determine whether a human's test plan fully captures the intended behavior.

The [local collector](../loop_engineering/controller.py) runs actual command checks against a fresh materialization, records exit status and output hashes, and checks for input mutation. It also rehashes the candidate before finalizing. This is useful operator-supervised evidence. It shares OS authority with the implementer and therefore reports `trusted_evidence: false`; host ownership remains the trust boundary. Native records are HMAC-authenticated with private keys outside the workspace; registered evaluator imports preserve original signatures and copied artifact hashes. The Docker check path isolates those authorities from the check mount when actual host conformance passes. See [native rules](native-engine.md) and [local limitations](local-controller.md#limits-and-trust).

## Acceptance contract

Each criterion has a stable ID, a behavioral description, and one or more check IDs. All checks listed in a 0.1 or 0.2 task are required. Each check must either be linked to a criterion or be an explicit global check, such as a required review.

| Check type | Appropriate evidence |
| --- | --- |
| `command` | Approved argv, bounded execution, exit code, preserved stdout/stderr and assertions |
| `interaction` | Exact user-flow steps, expected behavior, observed result, browser/app artifact references |
| `review` | Findings against criteria and diff; passing result only with no blocking findings |
| `human` | Identified authorized evaluator's attestation of the stated judgment |
| `artifact` | Approved inspection of an output file, rendered page, API response, or other deliverable |

Exit zero alone is insufficient when the command does not exercise the behavior. Tests must assert meaningful outcomes; a UI build does not prove a login flow works. An LLM review supplies judgment and can be wrong. Model agreement or identical patches is supplementary evidence, not an acceptance oracle.

Independent review is specified with `independent: true` on a review check. The collector must attest that it used a separate review context or an identified human who did not implement the change. A fresh context on the same model qualifies for role separation, though correlated mistakes remain possible. A model's self-declared independence is not accepted.

## Identities

Use SHA-256 of canonical JSON for contracts and structured records. v0.1 contract data contains integer numbers only, so cross-language canonicalization is small and explicit:

- UTF-8 encoding; object keys sorted by Unicode scalar value; array order preserved.
- Compact JSON with no whitespace; Unicode emitted directly; JSON control-character escaping.
- Integers restricted to -9007199254740991 through 9007199254740991 for exact cross-language representation.
- No floating-point numbers, nonfinite values, duplicate keys, invalid Unicode, or Unicode normalization during serialization.
- No self-referential digest field inside the object being hashed.

`canonical_digest` in the reference implements these rules for the JSON-compatible values it accepts. Other implementations must pass [the shared vectors](../examples/digest-vectors.json) before exchanging digests. JavaScript implementations must sort object keys by Unicode scalar value rather than default UTF-16 string ordering.

| Digest | Must identify |
| --- | --- |
| `contract_digest` | Entire authorized task JSON, including revision, criteria, checks, scope, and limits |
| `check_digest` | Entire approved definition of the individual check |
| `snapshot_digest` | Candidate manifest and actual code/input contents |
| `environment_digest` | Verification environment specification and relevant external fixture identity |
| Evidence record digest | Entire collector-produced evidence JSON, authenticated in the external ledger |

A Git commit SHA alone is not a candidate identity. It misses dirty tracked files, untracked task files, generated inputs, and submodule changes.

## Candidate manifest

The workspace service builds a sorted actual-byte manifest of included inputs: normalized relative path, kind, mode/executable bit, digest and size, plus the frozen exclusion-policy identity. It includes dirty/untracked files independently of Git status. Symlink identities can be captured, but materialization rejects them. Submodule provisioning is unsupported; choose a supported ordinary input tree or provide another verified workspace integration.

Ignore rules must be an approved part of the snapshot policy, not agent-editable exclusions. Outputs such as logs and `.loop/runs/` should stay outside the input manifest to avoid changing the snapshot just by collecting evidence. Include generated outputs if they are inputs to verification. Capture relevant ignored fixtures explicitly. For non-Git projects, the same manifest can identify the candidate.

Verify an immutable materialization of that manifest, or prevent writes during checks. Hashing a workspace before running tests on a mutable copy leaves a race. At release, compare the delivered or integrated candidate to the verified manifest and run required checks again if it changed.

The environment manifest should identify the runtime, dependencies/lockfiles, tools, sanitized configuration, test fixtures, platform, and relevant service state. Do not store secret values in evidence. Nondeterministic external services need a versioned fixture or explicit freshness/expiry policy; the digest by itself cannot freeze the world.

## Evidence records

See [evidence schema](../schemas/evidence.schema.json). A record contains task and check identity, candidate and environment digests, executor run ID, timestamps, result, exit code, artifact URIs with content digests, and a short factual summary.

The collector authenticates the executor or human, reads actual results, verifies artifact contents, and writes the record to a store the implementer cannot modify. The reference gate receives a set of record digests already validated by that collector. A JSON field saying `trusted: true`, a string claiming to be the executor, or a digest calculated by the agent is never sufficient authentication.

Review independence is similarly supplied as an externally verified subset of evidence record digests. The native import verifies registered key/role/check, purpose-specific MAC, unconsumed signed request, separate context, bindings, findings and actual artifact bytes. Records and original attribution survive in the authenticated projection. Do not give the implementer the collector's signing key or ledger write credential.

The gate consumes one selected final record per check. Keep old attempts in the event/evidence store but do not pass an ambiguous batch of historical and current results into final acceptance. Duplicate check IDs or evidence IDs are rejected. The controller selects a fresh batch by the approved run IDs; the implementer cannot cherry-pick an earlier passing attempt after a later failure on the same candidate.

## Gate algorithm

```text
assert task digest equals authorized contract digest
assert all criterion references identify declared checks
assert policy and scope inspection passed for candidate
assert interrupted effects have been reconciled
assert no blocking finding remains
for each required check:
    require exactly one current evidence record
    require externally authenticated record and artifact integrity
    require task, contract, check, snapshot, environment identities match
    require actual result is pass
    require command exit code is 0, or non-command exit code is null
    require independent attribution when specified
return PASS
```

Missing, stale, or inconclusive evidence returns `NEED_EVIDENCE`. A current authenticated failing check returns `REVISE`. An unauthorized contract, untrusted record, malformed or ambiguous batch, policy failure, unresolved effect, or blocking finding returns `REJECT`. The controller may diagnose a rejection; rejection itself does not grant a new permission or waive a check.

The gate evaluates the whole check set: it must not return success just because one criterion passed. It does not accept empty criteria or an empty check plan.

## Baselines, amendments, and flaky checks

Record baseline failures before implementation. By default a required failing check still prevents success. If a known unrelated failure should be tolerated, the authorized contract must identify the exact exception and alternative verification. v0.1 schemas deliberately include no implicit waiver field; revise the explicit check plan and explain the reason.

Do not mutate approved checks in place. A meaningful behavior/scope change needs the user's authorization. Within existing scope, the controller can record stronger checks or correct an unusable command without forcing repetitive permission prompts. Any contract revision changes the digest and conservatively invalidates evidence. This engine conservatively requires fresh evidence across changed contract/candidate/environment digests; stages derive passes from the integrated candidate rather than reusing stale slice results.

Protect the approved check plan outside the candidate workspace. An implementation may add tests but cannot remove the oracle, alter trusted fixtures, or disable checks. Run independently maintained checks where possible. If required checks depend on editable repository tests, review changes to those tests against the original behavioral criteria and inspect for weakened assertions or skipped execution.

Flaky checks need a predefined bounded policy, such as an approved sample count and threshold with all attempts preserved. Do not keep rerunning until one lucky pass appears. Without such a policy, a mixed result remains inconclusive or failing.

## Integration and release

A slice can pass while the combined result fails. Capture and verify the integrated candidate. Rebases, conflict resolution, build-input changes, or new review edits create a new snapshot and require fresh acceptance evidence.

Bind any approval for merge or publication to the candidate digest, target, action, and expiry. Do not apply approval to a later modified candidate. If the target branch changes, evaluate whether re-integration and re-verification are required before release. Success for a local coding task does not imply that these external actions happened.
