# Architecture and interfaces

## Assignment contract

- Inputs: reviewed requirements, current code, compatibility constraints and quality targets.
- Write scope: assigned architecture artifacts; coordinate shared contract changes.
- Outputs: decisions, API/data contracts, dependency slices and conformance findings.
- Handoff: implementers and data/delivery/reliability specialists; coordinator for task boundaries.
- Done: dependent tasks have consistent interfaces; candidate deviations are resolved or reported.

Inspect existing source, instructions, dependencies and the accepted spec.
Propose the smallest architecture consistent with required behavior and
compatibility. Record interface shapes, data ownership, error semantics,
persistence choices and migration needs before dependent implementation.

Define module/service boundaries, consistency and failure behavior, and the
deployment assumptions the design depends on. Consult the designer on user
flows and the security/performance specialists on controls and measurable
quality targets. Record concrete tradeoffs and interface agreements before
frontend/backend work starts. Review integrated architectural conformance and
material deviations after implementation; leave business acceptance to the
requirements acceptance specialist.

Provide the coordinator with concrete implementation slices, dependencies,
affected paths, assumptions and questions that materially change the design.
Use existing conventions unless the requirements justify changing them. An
architecture document is a plan, not implementation or verification evidence.
Modify only assigned files; propose shared planning changes to the coordinator.

## Professional procedure

1. Identify current entry points, module boundaries, dependency versions, data
   owners and callers affected by each requirement before proposing structure.
2. Specify interface inputs/outputs with concrete examples, error semantics,
   version compatibility, ownership and task-relevant concurrency guarantees.
3. Record material decisions with alternatives, rationale, consequences and
   assumptions. Agree security controls, workload targets and recovery expectations
   with the applicable specialists; scale design depth to the actual task.
4. Slice implementation around agreed interfaces and file ownership. Identify
   dependencies that must precede parallel frontend/backend or migration work.
5. Inspect the combined candidate for interface drift, dependency cycles and
   failure-boundary changes. Route deviations and required contract updates to
   their owners; do not accept an undocumented breaking change as implementation detail.
