"""Tools for a coordinator already running in Codex/Claude, without sampling."""

from .desktop_mcp import MCPServer, STRING, tool


def native_tool(name, description, properties, required, *, readonly=False, idempotent=False):
    result = tool(name, description, properties, required, readonly=readonly)
    result["annotations"]["idempotentHint"] = idempotent
    return result


TEAM = {"team_id": STRING}
TASK = {**TEAM, "task_id": STRING}
CHECK = {**TASK, "check_id": STRING}
TEXT = {"type": "string", "minLength": 1, "maxLength": 8192}

TOOLS = [
    native_tool("loop_workbench_reconcile_instructions", "Inspect or journal recovery of an original serial draft after an exact known published skill upgrade. dry_run=true reports original/new instruction hashes and stopped drafts; inspect them before supplying reviewed_changes. Only stopped/collected writers with no unresolved effects are eligible. Retains original child, frozen contracts, wait deadlines, all worker copies/provenance and cumulative usage. Never updates shared inputs, adds parallel policy or carries old evidence forward.",
                {**TASK, "workbench_id": STRING, "reviewed_changes": {"type": "array", "uniqueItems": True, "items": STRING},
                 "dry_run": {"type": "boolean"}}, ["team_id", "task_id", "workbench_id"], idempotent=True),
    native_tool("loop_workbench_refresh", "After actually inspecting the exact changed project paths, refresh a quiescent parallel draft onto the current candidate. Retains original draft, child, wait deadline and cumulative budgets. Path conflicts refuse; the original checks must run afresh after serial integration. Replays its original crash journal, never overwrites the shared project.",
                {**TASK, "workbench_id": STRING, "reviewed_changes": {"type": "array", "uniqueItems": True, "items": STRING}},
                ["team_id", "task_id", "workbench_id", "reviewed_changes"], idempotent=True),
    native_tool("loop_handshake", "Inspect the running framework fingerprint, protocol capabilities and installed instruction versions. Reconnect when disk code changed; frozen teams are never migrated by this read-only tool.", {}, [], readonly=True, idempotent=True),
    native_tool("loop_heartbeat", "Report actual current host coordination activity and its owner. Expires without another explicit report; read-only polling never renews it. This is not process attestation or permission to replay effects. Never synthesize an activity for another agent.",
                {**TEAM, "owner": STRING, "activity": TEXT, "task_id": STRING,
                 "ttl_seconds": {"type": "integer", "minimum": 15, "maximum": 300}},
                ["team_id", "owner", "activity"]),
    native_tool("loop_begin", "Initialize missing development controls and begin/reconnect a native-host team. No model is launched. Refuses takeover of an unfinished controller team; inspect progress first.",
                {"brief": {"type": "string", "minLength": 1, "maxLength": 16384}}, [], idempotent=True),
    native_tool("loop_progress", "Read actual stages, tasks, checks, questions, recent events and a refreshable local dashboard. No percentage or model cost is estimated.", TEAM, [], readonly=True, idempotent=True),
    native_tool("loop_next", "Prepare the next bound requirements/plan/task request, or report the actual questions, handoff, evaluator or stop condition. Follow next_action until the agreed milestone or a real blocker; no model is launched.",
                TEAM, ["team_id"], idempotent=True),
    native_tool("loop_project_supervise", "Record actual user authorization for a bounded sequence of successor batches. Batch COMPLETE is not whole-product completion. Retains prior evidence/usage and a cumulative controller-time cap; unknown host spend is not zero. Ordinary milestones enter requirements/planning before admission. Declare only already authorized scope and limits; never infer paid calls, release permissions or human acceptance from a roadmap. Repeated identical declarations are idempotent; scope/budget changes refuse.",
                {**TEAM, "objective": TEXT, "authorization": TEXT,
                 "controller_budget_seconds": {"type": "integer", "minimum": 1, "maximum": 2592000},
                 "milestones": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
                     "type": "object", "additionalProperties": False, "required": ["id", "brief"],
                     "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 64},
                                    "brief": {"type": "string", "minLength": 1, "maxLength": 16384}}}}},
                ["team_id", "objective", "authorization", "controller_budget_seconds", "milestones"], idempotent=True),
    native_tool("loop_project_advance", "Prepare the next authorized successor only after the latest batch has a current accepted candidate. Keeps predecessor COMPLETE, checks all previous effects, retains cumulative project accounting and creates an empty SPEC stage rather than replaying the completed workflow. A lost response reconnects the same successor; launches no model or worker. Follow loop_next using the returned team_id.",
                TEAM, ["team_id"], idempotent=True),
    native_tool("loop_project_control", "Pause, explicitly resume or cancel a registered project queue and its latest unfinished batch. Preserves completed predecessors and cumulative usage. Cancellation is terminal; never infer resume from reconnection.",
                {**TEAM, "action": {"enum": ["pause", "resume", "cancel"]}}, ["team_id", "action"], idempotent=True),
    native_tool("loop_project_budget_amend", "Record an actual user-authorized increase of the existing project queue's cumulative controller cap. Supply the current expected cap and new absolute cap, a stable amendment ID, actual authorization text and concrete reason. Keeps the original declaration, usage, contracts, review limits, effects policy and evidence unchanged; this is not permission for paid resources or missing capabilities. Root or successor IDs resolve to the same ledger. Identical retries recover the same amendment; stale expectations, conflicting IDs, decreases and new amendments to paused/cancelled projects refuse. Launches no model or worker and does not resume a stopped batch.",
                {**TEAM, "amendment_id": {"type": "string", "minLength": 1, "maxLength": 64},
                 "expected_controller_budget_seconds": {"type": "integer", "minimum": 1, "maximum": 2592000},
                 "controller_budget_seconds": {"type": "integer", "minimum": 1, "maximum": 2592000},
                 "authorization": TEXT, "reason": {"type": "string", "minLength": 1, "maxLength": 2048}},
                ["team_id", "amendment_id", "expected_controller_budget_seconds", "controller_budget_seconds",
                 "authorization", "reason"], idempotent=True),
    native_tool("loop_operation_status", "Read a durable background check/review operation. Optionally wait up to five seconds, keeping progress and pause/cancel responsive. FINISHED means the operation returned, not that the task passed; continue with loop_next.",
                {**TEAM, "operation_id": STRING, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 5}},
                ["team_id"], readonly=True, idempotent=True),
    native_tool("loop_operation_recover", "Reconcile an interrupted background operation only after its original runner has released ownership. Keeps the same child, cumulative budgets, checks and history; never blindly replays proposals or unpauses a user-stopped team.",
                {**TEAM, "operation_id": STRING}, ["team_id", "operation_id"], idempotent=True),
    native_tool("loop_role_context", "Read the configured specialist role and covering-owner instructions. Roles alone do not establish distinct agents or independent evidence.",
                {**TEAM, "role": STRING}, ["team_id", "role"], readonly=True, idempotent=True),
    native_tool("loop_stage_request", "Prepare bounded document-first requirements or coordination context and its exact response schema. Use this chat and native delegation to produce the response.",
                TEAM, ["team_id"], idempotent=True),
    native_tool("loop_plan_preflight", "Preview proposed task contracts before freezing: missing execution routes, declared measurement producers, actual unanswered decision IDs, and repair/rerun reserve. No keys, writes, checks or model calls. Resolve user decisions early and schedule engineering gaps; declarations never establish passing evidence.",
                {**TEAM, "contracts": {"type": "array", "minItems": 1, "maxItems": 128,
                 "items": {"type": "object", "additionalProperties": False,
                    "properties": {"task": {"type": "object"}, "engine": {"type": ["object", "null"]}},
                    "required": ["task", "engine"]}}}, ["team_id", "contracts"], readonly=True, idempotent=True),
    native_tool("loop_stage_submit", "Validate a response against the current bound stage request and journal spec/plan writes. Does not dispatch workers. Required evidence remains pending.",
                {**TEAM, "request_id": STRING, "response": {"type": "object"}}, ["team_id", "request_id", "response"]),
    native_tool("loop_plan_submit", "Submit a complete intake plan using native JSON objects for selection, workflow and task/engine contracts. Uses the same frozen stage validation and real admission gates; then follow next_action to dispatch ready work.",
                {**TEAM, "request_id": STRING, "selection": {"type": "object"}, "workflow": {"type": "object"},
                 "summary": TEXT, "contracts": {"type": "array", "maxItems": 128, "items": {
                     "type": "object", "additionalProperties": False,
                     "properties": {"path": STRING, "task": {"type": "object"}, "engine_path": {"type": ["string", "null"], "maxLength": 256}, "engine": {"type": ["object", "null"]}},
                     "required": ["path", "task", "engine_path", "engine"]}}},
                ["team_id", "request_id", "selection", "workflow", "contracts", "summary"]),
    native_tool("loop_repair_plan", "Repair only empty internal stage graphs of staged native tasks whose child never started. Journals a single stage owning all original criteria and scope; preserves contracts, checks, limits, task IDs, dependencies, delivered evidence and history. Refuses meaningful graph replacements, started children or stopped teams. No model is launched. dry_run=true only inspects eligibility.",
                {**TEAM, "dry_run": {"type": "boolean"}}, ["team_id"], idempotent=True),
    native_tool("loop_rework", "Recover a current authenticated independent review failure through its completed repair owner only under the frozen native_rework policy. Journals reopening of original children and affected descendants without changing scopes, checks, cumulative usage or limits. Refuses active effects/writers and exhausted allowances. Repeating the same failure recovers the original journal, never a new budget.",
                {**CHECK, "evidence_digest": STRING}, ["team_id", "task_id", "check_id", "evidence_digest"], idempotent=True),
    native_tool("loop_handoff_rework", "Recover the exact rejected native handoff under the existing frozen native_rework allowance. Retains actual rejected receipts, team/child identities, scopes, checks and cumulative usage; invalidates affected acceptance and requires a changed repair candidate. Receipt notes are host declarations, not independent verdicts. Refuses stopped teams/projects, unresolved writers/effects and exhausted allowances. dry_run=true inspects eligibility without dispatch or control-journal changes.",
                {**TASK, "handoff_id": STRING, "dry_run": {"type": "boolean"}},
                ["team_id", "task_id", "handoff_id"], idempotent=True),
    native_tool("loop_verification_begin", "Bind declared verification scenarios to a stable current workbench, actual host owner and observed environment. Returns a separate artifact directory and durable per-scenario status. This host-observation bridge executes no UI actions and supplies no independent pass. Reconnect to the same session; candidate changes invalidate old observations without resetting attempt limits.",
                {**TASK, "workbench_id": STRING, "owner": STRING, "environment": TEXT},
                ["team_id", "task_id", "workbench_id", "owner", "environment"], idempotent=True),
    native_tool("loop_verification_update", "Start a declared scenario before actual host actions, then report its actual observation with artifact paths relative to the session artifact directory. Repeated start returns the original attempt, never permission to repeat an effect. Report pass/fail/blocked against its exact attempt_id. Interrupted, expired, failed or blocked actions require reconcile with an observed state and explicit safe_to_retry before another attempt. Reports never replace original checks or independent evidence.",
                {**TEAM, "session_id": STRING, "scenario_id": STRING, "owner": STRING,
                 "action": {"enum": ["start", "pass", "fail", "blocked", "reconcile"]},
                 "attempt_id": STRING, "observation": TEXT,
                 "artifacts": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": STRING},
                 "safe_to_retry": {"type": "boolean"}},
                ["team_id", "session_id", "scenario_id", "owner", "action"], idempotent=True),
    native_tool("loop_verification_progress", "Read durable candidate-bound host verification observations, remaining attempts and unresolved actions. A stale or missing observation does not prove host liveness or acceptance. Inspect actual effects before retrying after interruption; no action or model is dispatched.",
                {**TEAM, "session_id": STRING}, ["team_id"], readonly=True, idempotent=True),
    native_tool("loop_planning_activity", "Record an actual host-declared planning assignment/result bound to the current stage request. Show real native agent IDs and actual reports; creates no implementation task and supplies no independent evidence.",
                {**TEAM, "request_id": STRING, "activity_id": STRING, "role": STRING,
                 "title": {"type": "string", "minLength": 1, "maxLength": 256}, "agent_id": STRING,
                 "status": {"enum": ["RUNNING", "COMPLETE", "BLOCKED"]},
                 "detail": {"type": "string", "minLength": 1, "maxLength": 2048}},
                ["team_id", "request_id", "activity_id", "role", "title", "agent_id", "status", "detail"]),
    native_tool("loop_answer_question", "Record an actual human answer, then refresh stage context. Never supply an invented answer.",
                {**TEAM, "question_id": STRING, "answer": TEXT}, ["team_id", "question_id", "answer"]),
    native_tool("loop_task_request", "Admit a ready task and return a frozen proposal context with role memory. Actual changes are serialized by Loop; native workers return proposals.",
                TASK, ["team_id", "task_id"], idempotent=True),
    native_tool("loop_task_submit", "Admit one scoped AgentStep and run its real checks in the background. Returns an operation ID: wait with loop_operation_status then continue with loop_next. Missing capabilities/evaluators refuse completion.",
                {**TASK, "request_id": STRING, "step": {"type": "object"}}, ["team_id", "task_id", "request_id", "step"]),
    native_tool("loop_workbench_prepare", "Prepare a request-bound disposable coding copy and recorded waiting deadline. Prefer actual file edits in this copy to a giant code JSON response; only the controller imports changes. Shares host OS authority, not containment.",
                TASK, ["team_id", "task_id"], idempotent=True),
    native_tool("loop_workbench_status", "Inspect actual draft file changes and the recorded host waiting deadline. Expiry means the host must stop empty waiting and inspect/take over or boundedly reassign; polling never renews the deadline. Drafts are not accepted evidence.",
                TASK, ["team_id", "task_id"], readonly=True, idempotent=True),
    native_tool("loop_workbench_submit", "Freeze a stable request-bound draft and run its scoped proposal and original checks in the background. Returns an operation ID: wait then follow loop_next. Accepts no arbitrary filesystem path or shell command; changed inputs, scopes or missing evaluators refuse completion.",
                {**TASK, "workbench_id": STRING, "summary": TEXT}, ["team_id", "task_id", "workbench_id", "summary"]),
    native_tool("loop_worker_assign", "Reserve a bounded specialist assignment and separate copy inside a current task workbench. Exact file ownership, collected dependencies, selected role and frozen max_parallel apply. PREPARED has no agent yet: the host spawns one and records its actual ID. No model is launched.",
                {**TASK, "workbench_id": STRING, "assignment_id": STRING, "role": STRING,
                 "title": {"type": "string", "minLength": 1, "maxLength": 256}, "goal": TEXT,
                 "write_paths": {"type": "array", "maxItems": 128, "items": STRING},
                 "depends_on": {"type": "array", "maxItems": 128, "items": STRING}},
                ["team_id", "task_id", "workbench_id", "assignment_id", "role", "title", "goal", "write_paths", "depends_on"], idempotent=True),
    native_tool("loop_worker_update", "Bind the actual host agent ID and report actual running/result/blocker/stopped state. Draft-ready is not task completion. Stopped means the host actually stopped the writer; the service interrupts no model. Updates never reset deadlines.",
                {**TEAM, "worker_id": STRING, "agent_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 256},
                 "status": {"enum": ["RUNNING", "DRAFT_READY", "BLOCKED", "STOPPED"]},
                 "detail": {"type": "string", "minLength": 1, "maxLength": 2048}},
                ["team_id", "worker_id", "agent_id", "status", "detail"]),
    native_tool("loop_worker_collect", "Serially collect a stable returned/stopped specialist draft into its coordinating workbench. Validate exact paths and file preconditions; crash retries use the frozen import journal. Shared project effects and authoritative checks only occur on later task submission. Read-only advice supplies no independent pass.",
                {**TEAM, "worker_id": STRING, "summary": TEXT}, ["team_id", "worker_id", "summary"], idempotent=True),
    native_tool("loop_dispatch_board", "Show actual registered specialist assignments, host-declared agent IDs, observed files, collected dependencies, occupied/free slots and deadlines. Stale running agents occupy slots until the host stops them. The host supervises recovery and refills ready professional work within its actual capacity.",
                TEAM, ["team_id"], readonly=True, idempotent=True),
    native_tool("loop_collect_task", "Recover/collect actual task results and prepare a bound handoff. A task with outstanding checks remains unfinished.",
                TASK, ["team_id", "task_id"]),
    native_tool("loop_receive_handoff", "Record an assigned recipient's actual inspection of the bound outputs and checks. This receipt is host-declared, not independent evaluator evidence.",
                {**TASK, "handoff_id": STRING, "role": STRING, "accept": {"type": "boolean"}, "note": TEXT},
                ["team_id", "task_id", "handoff_id", "role", "accept", "note"]),
    native_tool("loop_team_control", "Explicitly pause/cancel a project team or resume a native team. Cancellation is terminal. Never cancel an existing team solely to switch modes without the user's instruction.",
                {**TEAM, "action": {"enum": ["pause", "resume", "cancel"]}}, ["team_id", "action"]),
    native_tool("loop_memory", "Read bounded source-linked role memory from actual checkpoints. Advice and host reports do not attest completion.",
                {**TEAM, "role": STRING, "task_id": STRING}, ["team_id"], readonly=True, idempotent=True),
    native_tool("loop_evaluation_request", "Prepare a signed request for an admitted task's registered non-command evaluator. Exports no signing keys.", CHECK, ["team_id", "task_id", "check_id"]),
    native_tool("loop_evaluation_import", "Import an actual registered evaluator's signed result, checking request/candidate identity, signature, artifacts and replay. An unsigned chat verdict is refused.",
                {**TASK, "envelope": {"type": "object"}}, ["team_id", "task_id", "envelope"]),
    native_tool("loop_evaluation_run", "Run a registered supervised browser/HTTP executor or independent Codex reviewer in the background; returns an operation ID. Wait then follow loop_next. Reviews use a fresh bounded read-only CLI context and actual signed results. Cached results do not redispatch. Only after material/permission repair or an authorized bounded retry use retry=true; cumulative limits remain. Missing registration, authority, runtime or accounting refuses dispatch; accepts no shell command.",
                {**CHECK, "retry": {"type": "boolean"}}, ["team_id", "task_id", "check_id"]),
]


class NativeMCPServer(MCPServer):
    tools = TOOLS
    server_name = "loop-native"
    instructions = "Start with loop_handshake; reconnect changed framework code before mutations and preserve customized instructions/frozen batches. Report actual host activity with loop_heartbeat, never infer liveness from polling. Opted-in native_parallel plans can admit disjoint ready task drafts, while imports/checks remain serial; inspect changed paths before loop_workbench_refresh and preserve original deadlines. Dependency reuse applies only to recorded host observations with complete declared coverage, never formal evidence. Budget registered reviewer time and repeated command checks before freezing. Batch unresolved material user decisions during intake and reuse actual recorded answers. Preview proposed contracts with loop_plan_preflight before freezing; distinguish missing engineering from user decisions and external acceptance capabilities. Submission/evaluation tools return background operation IDs: boundedly wait with loop_operation_status, recover interrupted journals with loop_operation_recover, then follow loop_next through scoped repair and acceptance. FINISHED operations are not passing tasks. Own coordination in this conversation: call loop_next, prepare a task workbench, agree common interfaces, then split useful professional jobs with loop_worker_assign and explicit disjoint files. Spawn actual native workers within both board max_parallel and host capacity; bind IDs with loop_worker_update. Inspect loop_dispatch_board, collect returned drafts promptly with loop_worker_collect, refill ready work and recover expired/blocked writers instead of empty waiting. Submit the collected task with loop_workbench_submit for original checks and handoffs. Keep the same team/child and deadlines. For a workflow that opts into native_rework, follow loop_rework on a bound failed review and preserve original child budgets. For declared verification_scenarios, use loop_verification_begin/update/progress to retain actual candidate-bound observations; inspect unresolved effects before retry and never treat host reports as independent evidence. Inspect verification_preflight before promising final acceptance; a signing key alone is not a running reviewer. Ordinary coordination launches no model. Only loop_evaluation_run with explicit private review registration may launch an independent bounded CLI reviewer. Native OS authority and host-chat usage remain outside containment/accounting. Missing measurement producers, telemetry, fixtures and result-import paths are authorized engineering work within the original scope and limits. Missing external evaluators or human judgments block affected checks; continue independent authorized work, retain pending evidence and never relabel machine results as human. Before ending an unfinished turn, inspect loop_next and follow executable actions; progress reports and gap audits are checkpoints. Progress includes turn_boundary: may_end_turn=false means the recorded route requires continued work even if every worker returned or regression tests passed. An unresolved product defect or missing measurement interface needs scoped repair, or a concrete reviewed recovery proposal if original guards/scope prevent repair. Stop gates preserve missing evidence and do not prove engineering exhaustion. This signal cannot attest host liveness or wake an ended conversation. Actual user input, pause/cancel, exhausted limits and milestone boundaries remain gates. Distinguish batch completion from the overall user goal. For actual authorization of a bounded successor sequence, register loop_project_supervise with the reviewed cumulative controller budget; after COMPLETE follow advance_project via loop_project_advance and use its returned new team ID for requirements/planning. Preserve accepted predecessors and prior usage; never replay their workflows or treat queue exhaustion as proof of unlisted product scope. Follow existing successor links on reconnect and use loop_project_control for actual whole-project pause/resume/cancel. If early plan preflight exposes insufficient cumulative allowance, prepare its exact shortfall and absolute cap; only actual user authorization permits loop_project_budget_amend with a stable amendment ID and expected current cap. Keep the same original queue/accounting and per-child checks; an amended cap grants no paid resources, review attempts or new scope. The host still owns continuing this turn; the queue launches no coordinator model."

    instructions += " A rejected current handoff may return rework_handoff or recover_rework bound to loop_handoff_rework under the original frozen native_rework allowance. Follow the exact handoff ID, recover the same journal and preserve receipt findings as host declarations. Reopen the original authorized owner, require a changed repair candidate and repeat original checks, independent reviews and new recipient acceptance. Do not use ordinary team resume or a replacement batch to bypass this scoped recovery."

    instructions += " When an exact published skill upgrade makes a serial draft stale, inspect_instruction_upgrade uses loop_workbench_reconcile_instructions dry_run=true. Inspect original/new hashes and actual stopped writers, then follow the returned arguments. Recover its original instruction refresh journal after interruption. Keep original child, limits, deadlines and all stopped drafts; collect their original worker IDs into the replacement copy and run the original checks. Customized instructions or unrelated source changes refuse this route."

    def handlers(self):
        s = self.service
        def watched(method):
            def call(**args):
                if args.get("team_id"):
                    s.watch_progress(args["team_id"])
                result = method(**args)
                if isinstance(result, dict) and result.get("team_id"):
                    s.watch_progress(result["team_id"])
                    if "dashboard_path" in result:
                        result["dashboard_url"] = s.dashboard_url(result["team_id"])
                if isinstance(result, dict) and "teams" in result:
                    for state in result["teams"]:
                        s.watch_progress(state["team_id"])
                        state["dashboard_url"] = s.dashboard_url(state["team_id"])
                return result
            return call
        return {name: watched(method) for name, method in {
            "loop_handshake": s.handshake, "loop_heartbeat": s.heartbeat,
            "loop_workbench_refresh": s.workbench_refresh,
            "loop_workbench_reconcile_instructions": s.workbench_reconcile_instructions,
            "loop_begin": s.begin, "loop_progress": s.progress, "loop_next": s.next,
            "loop_project_supervise": s.project_supervise, "loop_project_advance": s.project_advance,
            "loop_project_control": s.project_control, "loop_project_budget_amend": s.project_budget_amend,
            "loop_operation_status": s.operations.status, "loop_operation_recover": s.operations.recover,
            "loop_role_context": s.role_context, "loop_stage_request": s.stage_request,
            "loop_stage_submit": s.stage_submit, "loop_plan_submit": s.plan_submit,
            "loop_plan_preflight": s.plan_preflight,
            "loop_repair_plan": s.repair_plan,
            "loop_rework": s.rework,
            "loop_handoff_rework": s.handoff_rework,
            "loop_verification_begin": s.verification_begin,
            "loop_verification_update": s.verification_update,
            "loop_verification_progress": s.verification_progress,
            "loop_planning_activity": s.planning_activity, "loop_answer_question": s.answer,
            "loop_task_request": s.task_request, "loop_task_submit": s.task_submit_background,
            "loop_workbench_prepare": s.workbench_prepare, "loop_workbench_status": s.workbench_status,
            "loop_workbench_submit": s.workbench_submit_background,
            "loop_worker_assign": s.worker_assign, "loop_worker_update": s.worker_update,
            "loop_worker_collect": s.worker_collect, "loop_dispatch_board": s.worker_board,
            "loop_collect_task": s.collect, "loop_receive_handoff": s.receive,
            "loop_team_control": s.control, "loop_memory": s.memory,
            "loop_evaluation_request": s.evaluation, "loop_evaluation_import": s.evaluation,
            "loop_evaluation_run": s.evaluation_background,
        }.items()}
