## Playbook: develop

Goal: working software that passes the approved acceptance checks.

- **Plan.** Read the objective, constraints and listed context files first. Split the work into
  at most 20 tasks, each one result with its own checks (at most 6 requirements and 8 checks).
  Use real commands such as `npm test -- auth` or `pytest tests/test_api.py`, never `echo ok`.
  Set `depends_on` only where one task really needs another; independent tasks run in
  parallel. Prefer vertical slices that can be verified.
- **Work.** Work on the task (or the parallel batch) in your assignment. Read the code before
  changing it. Make the smallest coherent change, run the relevant tests yourself, then call
  `loop_task(action="done")`. The controller runs the task's checks. A failed check means the
  task is not done yet: read the failure output and fix the cause. A task that stays stuck:
  split it with `loop_plan` instead of retrying the same thing.
- **Repair.** When final acceptance fails, fix the root cause shown in the failing output. Do
  not weaken, skip or delete tests or checks. If a check is genuinely wrong, ask the human
  with `loop_ask`.
- **Quality.** Keep the repository buildable after every task. Match the existing style.
  Add or update tests for behavior you change. Commit only if the constraints say to.
- **Parallel help.** When the assignment lists parallel tasks, give each one to a sub-agent with
  its own files. The controller only sees the result through tasks and checks.
