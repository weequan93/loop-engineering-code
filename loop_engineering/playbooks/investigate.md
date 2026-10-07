## Playbook: investigate

Goal: find the root cause of a problem, with evidence another engineer can reproduce.
Optionally, fix it.

- **Reproduce first.** Find the smallest command that shows the problem. Record it, with its
  output, as `loop_note(kind="evidence")`.
- **Hypotheses.** Record each candidate cause as `loop_note(kind="hypothesis")`. Test the
  cheapest discriminating hypothesis first. Mark each one confirmed or rejected with
  `finding_id` and `finding_status`, citing the experiment.
- **Challenge the obvious label.** "Permission denied", "flaky" or "environment" is a
  symptom, not a cause. Check platform behavior and the exact syscall or command, then try a
  direct workaround before calling it external. Example: macOS returns EPERM from killpg for
  a group that holds only zombies, and sandboxes can refuse setuid tools such as /bin/ps.
- **Root cause.** When the evidence supports it, record `loop_note(kind="root_cause")`. Then
  either fix it (add tasks with checks) or explain why the fix needs a human decision.
- **Report.** Finish with `loop_finish(report=...)`. The report covers the symptom, the
  reproduction, the hypotheses tried, the root cause, the fix or recommendation, and the
  remaining risk.
