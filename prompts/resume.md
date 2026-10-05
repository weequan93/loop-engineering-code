# Resume prompt

Continue the existing task using `LOOP.md`, `.loop/task.json`, and `.loop/handoff.md`. The original conversation may be unavailable.

Inspect the real workspace and repository instructions. Reconcile outstanding actions before retrying them, confirm which snapshot the saved evidence checked, and identify capability differences in this session. Preserve the task revision and cumulative budgets. Treat handoff claims as factual leads to verify against artifacts.

Continue from the next useful action if it still fits the current authorized contract. If code, environment, or contract changed, obtain fresh verification. Save the updated handoff and report a precise result or resumption condition.

For the controller bridge, use a fresh `context` export after `resume`; its snapshot and file hashes supersede saved proposals. Return one AgentStep rather than editing the workspace. An uncertain dispatch must be reconciled before another attempt, and an exhausted implementation budget can still allow verification of the already dispatched candidate.
