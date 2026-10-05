"""Failure categories and bounded responses, independent of provider names."""


def recovery_policy(category: str) -> dict:
    policies = {
        "code": ("replan", "Use the failing assertion and a different diagnostic hypothesis; verify the next candidate."),
        "transient": ("bounded_retry", "Retry at most once within the remaining budget; retain both attempts."),
        "dependency": ("BLOCKED", "Identify the unavailable dependency and an observable resumption condition."),
        "permission": ("AWAITING_INPUT", "Continue permitted work; name the exact denied action if authorization is required."),
        "authentication": ("AWAITING_INPUT", "Restore backend authentication through its normal login flow."),
        "flaky": ("BLOCKED", "Use an explicitly approved sampling policy; do not rerun until one attempt passes."),
        "invalid_output": ("replan", "Repair the response format once without changing the task contract."),
        "unknown_effect": ("AWAITING_INPUT", "Reconcile actual effects before replay; preserve user changes."),
    }
    action, instruction = policies.get(category, ("BLOCKED", "Preserve diagnostics and identify the missing capability."))
    return {"category": category, "action": action, "instruction": instruction}
