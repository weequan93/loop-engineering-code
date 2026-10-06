# Starter prompt

Use the portable coding loop in `LOOP.md` for the task in `.loop/task.json`. Read the repository's existing instructions first and preserve existing user changes.

Inspect the baseline and identify the actual available capabilities. Choose the lightest workflow that fits. Make the smallest useful change or diagnostic, run meaningful checks against the actual candidate, and record factual evidence. Keep `.loop/handoff.md` current so another agent or model can continue without this conversation.

Confirm non-goals, assumptions, dependencies, and compatibility requirements before implementation. Protect approved acceptance tests. Reserve enough budget for final verification. If a ContextBundle is supplied by the local controller, return only an AgentStep matching the supplied schema and negotiated task version (default 0.2, opt-in 0.3); the controller owns edits, checks, and durable state.

Proceed with authorized work. Ask only for material missing input, continue independent work while waiting, and honor cumulative limits. Do not silently change acceptance criteria, claim checks that did not run, or repeat an ineffective attempt without a new diagnosis.

Finish with the change, verification, remaining limitations, and a recoverable checkpoint. In a native integration, submit a completion request for the controller to evaluate.
