# Adopt the framework

## Development scenario

Use `init /path/to/project --scenario development --spec /path/to/spec.md`
through the repository's CLI, then ask your coding host to read `.loop/start.md`.
The preset supplies a specialist roster and a coordinator workflow that owns
requirements clarification, task preparation, assignments, integration and
verification. Users provide a spec and answer material questions. See
[scenario presets](scenarios.md) for the exact commands and host requirements.

## Use the implemented CLI

Start with [the complete usage guide](how-to-use.md) or [the local controller guide](local-controller.md) for setup, `init`, `doctor`, context exports, proposal submission, configured adapters, and recovery. `init` creates missing workflow files and a project profile without executing discovered commands or replacing existing agent instructions. Replace the generated task scaffold before starting. Store controller state outside the project.

Use direct portable instructions below when the coding tool already owns editing/execution or when a human applies a chat model's proposed changes. The controller bridge uses proposal-only mode instead: the model returns JSON, and the controller owns effects and state.

## Existing coding tool

Use this layout inside the target project:

```text
LOOP.md                   # stable portable workflow
.loop/task.json           # current task contract
.loop/handoff.md          # concise resume state
.loop/runs/<run-id>/       # check logs and local artifacts
```

Merge [the pointer](../templates/agent-instructions.md) into the instruction file the host actually reads. Do not replace existing instructions. If the host has no instruction loader, paste the pointer and task into the session. No installation into a provider-specific plugin is required.

Adapt [the task template](../templates/task.json). Replace every sample requirement and path, choose meaningful checks, and use the repository's actual commands. Start with `quick` for a small known fix and `standard` otherwise. Default to assisted operation until the host controls are understood. Use `manual` when checks or snapshots are performed by a human.

Send [the start prompt](../prompts/start.md), with the real file locations. The agent should inspect the baseline, make bounded progress, run the applicable checks, and save the handoff. A declared budget in JSON remains advisory if the host does not enforce it.

The native controller should store authoritative task/evidence/state outside the implementer's writable workspace. The `.loop/` layout above is a convenience for portable assisted use, not a trusted native control store.

## Example task

The sample task fixes a Python slug function so all-whitespace input returns the empty string and existing behavior stays intact. The two criteria are linked to a regression check and the existing suite. [A small deliberately buggy application](../examples/slug-project/) now contains executable checks, protected from proposal edits by its task scope. The general task template still needs adaptation to your real repository and paths.

The agent might begin with:

```text
Objective: whitespace-only input produces an empty slug.
Baseline: reproduce the failure and record the existing suite result.
Step: change whitespace handling and add a behavioral regression check.
Verify: run regression and relevant suite on the resulting snapshot.
Checkpoint: preserve patch, results, pending criteria, and next action.
```

If the existing suite already fails, preserve the baseline evidence and diagnose whether the failure is related. Do not silently drop a required check. Update the contract only through the controller or the user's authorized intent.

For a UI task, replace the criteria with actual user-visible behavior and add an `interaction` check with steps and expected observations. Register a matching browser `interaction` executor for controller-owned automated interaction; require agent `browser_interaction` only when the implementing host itself must operate the browser. Native text drivers do not gain that agent capability from an external executor. If the model cannot inspect an image, use an identified human visual judgment where appropriate rather than claiming image verification occurred.

## Switching agent or model

Save the latest candidate as recoverable files, patch, or an authorized Git checkpoint. Fill the handoff with exact task revision, snapshot identity, passed/pending criteria, logs, failed approaches, outstanding actions, and one next step.

Open a new session with [the resume prompt](../prompts/resume.md). The next agent reads the task and handoff, checks the real repository state, re-negotiates capabilities, and verifies that the saved evidence matches. It can then continue without the prior chat. Do not reset budgets or rerun an external operation whose effect is unknown.

## Chat-only model

Paste the objective, relevant source, acceptance criteria, and the portable loop instructions. Ask for one coherent patch and the exact check procedure. Apply the patch yourself, execute the checks, and return the actual result. Maintain the same handoff.

Label this `manual`. The model can participate in the workflow but cannot attest that a check ran on your computer. A model that fails to follow the contract consistently is unsuitable for autonomous operation even when the format is portable.

## Native engine

The [local controller](local-controller.md) implements the file broker, durable store, snapshot capture, check collector, and gate mechanics of the stage 2 vertical slice in [architecture](architecture.md). It has real file/process recovery tests and a scripted command bridge example. Add protected execution and authenticated evidence before relying on unattended operation; then evaluate a real backend and a second provider before adding a scheduler.

Use the reference core as executable examples of decision semantics. Integrate a real schema validator, authenticated manifests, artifact hashing, budget reservations, process supervision, and the state machine before relying on it for unattended work.

## Local reference validation

```bash
python3 -m reference.demo
python3 -m unittest discover -s tests -p test_core.py -v
```

For the CLI, integration tests, and full JSON Schema validation, install the dependency in an environment you manage:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 scripts/validate_contracts.py
python3 scripts/demo_local.py
```

The framework's decision core has no external Python dependency. The schema utility fails with a clear dependency message rather than silently skipping validation.
