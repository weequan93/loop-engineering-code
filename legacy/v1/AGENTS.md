# Maintaining this framework

The current development order is implementation first, live evaluation last.

- Do not run live coding-agent or model-provider tests while required framework definitions, implementation, or offline coverage remain unfinished. Track those separately in `docs/implementation-status.json`; consult `docs/implementation-status.md` for scope and sequence.
- Use deterministic proposals, injectable fake provider transports, local check subprocesses, and crash/cancellation fixtures during development. Keep the default test suite and CI free of live model dispatch.
- Before a future live evaluation, run `python3 scripts/check_readiness.py --for-live-evaluation` and the full offline validation. A nonzero readiness result blocks that evaluation. A passing inventory does not start a provider automatically or certify runtime containment.
- Update inventory entries only after the relevant code is integrated and its offline cases pass. Do not mark design notes, unsupported stubs, or a source reference alone as implemented.
- Preserve the supervised fallback and fail closed on missing runtime/evaluator/accounting capabilities. State actual platform and provider limitations in documentation.

The human user's current instructions take precedence over this development policy. These instructions govern work on this repository; reusable `LOOP.md` and exported project templates have their own coding-task verification rules.
