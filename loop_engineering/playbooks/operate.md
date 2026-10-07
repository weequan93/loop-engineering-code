## Playbook: operate

Goal: keep a running system healthy. The controller runs the health checks (the acceptance
checks) on a schedule. You are called only when a check fails (an incident) or when an
operations task is ready.

- **Triage first.** Read the failing check output in the assignment. Collect current
  evidence: logs, metrics, recent deploys, config diffs. Record it with
  `loop_note(kind="evidence")` before you change anything.
- **Remediate within policy.** Safe, reversible actions are fine without asking: restarting
  your own service, clearing a cache you own, rolling a config back to its last known good
  version. Any action listed under "needs approval" requires
  `loop_ask(kind="approval", action=...)` first. Wait for the answer; do not perform it in
  the same turn.
- **Verify.** After remediation, end your turn. The controller re-runs the health checks and
  either resolves the incident or calls you again with the new output.
- **Follow-up.** For a fix that needs code changes, add a task with `loop_plan` and leave the
  incident handled by a safe mitigation. Write a short incident note: cause, impact, action,
  and prevention.
- **Never** silence an alert, delete evidence, or mark health green by editing checks.
