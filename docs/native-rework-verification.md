# Native review recovery and verification sessions

This extension is supervised host orchestration. It does not launch a model, wake a stopped host, grant OS permissions, or make host observations independent evidence.

## Bounded review recovery

New workflows may opt in with `native_rework: {"max_rounds": 3}`. Each reviewing task declares its existing `repair_task`. A current authenticated failed independent review may then reopen that owner and invalidate its descendants through `loop_rework`. Absent policy means no automatic reopening of completed work. This does not amend a running project's frozen inputs.

Recovery keeps the team and child IDs, criteria, scope, usage, iteration counters, original reviews and previous handoffs. It journals the affected records before changing children and resumes that same journal after interruption. Running effects, active writers, unresolved verification actions, stopped teams, exhausted child allowances, stale evidence and unauthorized owners refuse recovery. A repaired owner must deliver a different candidate before downstream checks proceed. Repeated requests for the same failure do not consume another round. The effective allowance is capped by the workflow and remaining frozen team reworks, including recorded preparation repairs.

Large findings sets are retained in a private JSON file with a content hash. Task packets carry a bounded preview and the full file reference, rather than repeating the entire review inside the mandatory context. The coordinator must read all findings; a preview is not the complete repair scope. Original authenticated evidence remains in the same child history.

## Host verification sessions

A task can declare `extensions.verification_scenarios`, containing bounded, dependency-ordered scenarios linked to its original checks. `loop_verification_begin` binds a session to the current workbench candidate, actual host owner and reported environment. Artifacts are written in its separate artifact directory, so captures do not mutate the coding candidate.

The host starts each scenario before its actual actions, records observations and artifact paths afterward, and consults durable progress after reconnect. A running attempt cannot be restarted or implicitly passed. Explicit reconciliation records the observed state and whether retry is safe. Candidate changes make earlier observations stale. Attempt limits and history survive reconnect; a new session cannot erase an unresolved action.

These records describe host-observed progress only. Original command checks, authenticated external evidence and independent review remain required for acceptance. The host must verify environment identity and actual effects; this service neither controls a browser/emulator nor independently attests a host report. Unknown activity after interruption requires inspection rather than blind replay.

Example extension on a newly prepared task (replace the check ID with its actual check):

```json
{
  "verification_scenarios": [
    {
      "id": "android-connectivity",
      "check_id": "client-integration",
      "surface": "android-emulator",
      "title": "Install, log in and observe one backend round trip",
      "kind": "capability",
      "depends_on": [],
      "max_attempts": 3,
      "timeout_seconds": 300
    }
  ]
}
```

Scenario checks/dependencies are validated before freezing. Attempt counts are cumulative across changed candidates and sessions. Failed and blocked reports also require explicit effect reconciliation before retry; a negative safety decision cannot be bypassed with a new session. A pass requires nonempty, bounded artifacts captured into the private content store. Only relative paths inside the returned session artifact directory are accepted. Interrupted actions prevent both proposal and workbench submission, and prevent new specialist dispatch for that task.

Inspection-only `reconcile` reports are allowed after pause/cancel; they never resume the team or permit a new action. An unresolved action in a cancelled batch prevents starting a replacement batch until its original effects are inspected and reconciled.

The progress page shows surface, scenario, owner, observed status and last observation time. It does not infer host liveness from the controller being ACTIVE. Scenario observations are supplementary progress: they never create formal evidence or bypass the task's original gate. The [native autonomy extension](native-autonomy.md) adds opt-in concurrent draft admission and dependency-aware observation reuse; imports, formal checks and final acceptance remain serial and bound to the integrated candidate.

## Planning

Ask unresolved material scope, environment, permission and budget questions during intake. Reuse existing actual answers. Include launch/install/login/backend capability smoke scenarios before lengthy client work, and final scenarios under the task that verifies the integrated candidate. Keep writes and scenarios within the approved scope. A technical failure within that scope should produce a repair assignment rather than a generic request to continue.

Implementation and offline validation are required before live evaluation. See the implementation inventory for current completion status.
