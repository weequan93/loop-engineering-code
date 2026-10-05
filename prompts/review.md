# Independent review prompt

Review the authorized task, original behavioral criteria, current diff, candidate identity, and actual evidence in a context separate from implementation. Inspect whether the change satisfies the task and whether it introduces relevant regressions. Examine changed tests or fixtures for weakened verification.

Return concrete findings tied to files or criteria, with severity, evidence, and the check needed to resolve them. State which checks you actually ran or inspected. If no blocking finding remains, report that judgment with its limitations. Do not rewrite the criteria or turn absent evidence into a pass.

Your response is a review input. The host collector authenticates the review and the controller decides completion.
