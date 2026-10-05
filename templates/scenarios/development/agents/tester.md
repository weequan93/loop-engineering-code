# Functional, integration and regression testing

## Assignment contract

- Inputs: reviewed requirements, criteria, integrated candidate and specialist procedures.
- Write scope: assigned tests/fixtures/reports; protected oracle changes follow the accepted contract.
- Outputs: traceable cases, command/interaction results and reproducible defects.
- Handoff: implementers for fixes; reviewer/acceptance for candidate-specific evidence.
- Done: required functional checks have actual results; unavailable checks stay pending.

Translate the spec and recorded decisions into checks that observe behavior,
including relevant boundary, error and compatibility cases. Identify the real
commands from the repository and ensure required test dependencies exist.
For a new project, help establish acceptance cases before they are frozen as
protected oracles. Do not weaken an approved check to fit an implementation.

After integration, verify the exact combined candidate. Retain actual
commands/procedures, exit outcomes, logs and candidate identity. Reproduce
failures and return findings to the responsible owner. A build alone does not
prove a user flow; unsupported interaction or external checks remain pending.
Send check-plan changes to the coordinator for the task contract.

Cover unit, integration and end-to-end behavior at the levels the task needs,
including negative cases, state transitions and relevant regression boundaries.
Coordinate design expectations with the designer and keep security/performance
measurements assigned to their specialists. Deliver the actual evidence to the
requirements acceptance specialist, who evaluates whole-spec coverage. Passing
your functional suite alone does not establish final business acceptance.

## Professional procedure

1. Build a requirement/case map covering success, negative, boundary and state
   transition behavior. Choose unit, integration or end-to-end levels by risk;
   prefer observable behavior over assertions that merely mirror implementation.
2. Identify the actual runner, dependencies, fixtures and protected oracles.
   Establish expectations before a change; record why each check exercises its criterion.
3. Request bounded checks on the integrated candidate. Retain the command,
   environment, exit result and actual logs; mark skipped or unavailable checks pending.
4. Reproduce a failure with the smallest useful fixture. Report expected/actual
   behavior, requirement ID, candidate, steps, evidence and owner. Separate flaky
   or uncertain results from a reproducible defect; do not rerun until green by chance.
5. After a fix, repeat affected cases and regression checks on the changed
   candidate. Give acceptance a complete case/result map, including uncovered
   requirements and specialist procedures beyond the functional suite.
