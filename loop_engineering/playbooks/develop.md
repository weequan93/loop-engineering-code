## Playbook: develop

Goal: working software that passes the approved acceptance checks.

- **Plan.** Read the objective, constraints and listed context files first. Split the work into
  2–15 tasks. Give each task a clear outcome and one or more fast checks. Use real commands
  such as `npm test -- auth` or `pytest tests/test_api.py`, never `echo ok`. Order tasks with
  `depends_on`. Prefer vertical slices that can be verified.
- **Work.** Do one task at a time. Read the code before changing it. Make the smallest coherent
  change, run the relevant tests yourself, then call `loop_task(action="done")`. The
  controller runs the task's checks. A failed check means the task is not done yet: read
  the failure output and fix the cause.
- **Repair.** When final acceptance fails, fix the root cause shown in the failing output. Do
  not weaken, skip or delete tests or checks. If a check is genuinely wrong, ask the human
  with `loop_ask`.
- **Quality.** Keep the repository buildable after every task. Match the existing style.
  Add or update tests for behavior you change. Commit only if the constraints say to.
- **Parallel help.** If your host supports sub-agents, you may use them inside your turn. The
  controller only sees the result through tasks and checks.
