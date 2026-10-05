"""Derive the next host action from recorded state, without dispatching a model."""


def next_action(state, data):
    def action(kind, summary, *, tool=None, arguments=None, proceed=False, **extra):
        return {"kind": kind, "summary": summary, "tool": tool,
                "arguments": arguments or {}, "continue_work": proceed, **extra}

    team = {"team_id": state["team_id"]}
    if "native_host" not in data:
        return action("existing_controller", "原团队保留原执行入口和预算；当前对话不能接管。")
    status = state["status"]
    if status in {"PAUSED", "CANCELLED", "BLOCKED"}:
        return action(status.lower(), state["reason"] or {
            "PAUSED": "已暂停，等待用户明确继续。", "CANCELLED": "已取消，保留历史记录。",
            "BLOCKED": "需要处理实际阻塞，不能声明完成。"}[status])
    if status == "COMPLETE":
        return action("complete" if state["final_candidate_current"] else "stale_completion",
                      "本轮合同已验证完成。" if state["final_candidate_current"] else "完成记录对应的候选已变化。")
    if state.get("compatibility", {}).get("restart_required"):
        return action("reconnect_framework", "框架文件已更新，重新连接 MCP 后核对原团队和未完成操作；保留冻结批次与历史。")
    plan_configuration = state.get("plan_configuration", {})
    if plan_configuration.get("pending"):
        return action("repair_plan", "恢复原阶段配置修复日志；保留原团队、任务编号、预算和交付记录。",
                      tool="loop_repair_plan", arguments=team, proceed=True)
    if not state["inputs_current"]:
        return action("input_conflict", state["input_problem"] or "冻结资料已变化，需要先处理冲突。")
    if state.get("evidence_problems"):
        return action("evidence_problem", "已记录的检查产物存在问题，先处理实际证据。")
    pending = [q for q in state["pending_questions"] if q["blocking"] and not q["answers"]]
    if pending:
        return action("answer_questions", "等待用户回答剩余需求问题。", questions=pending)
    if state["stage"] in {"SPEC", "INTAKE"}:
        stage = state["stage"]
        return action("prepare_spec" if stage == "SPEC" else "prepare_plan",
                      "收集实际资料并提交需求结果。" if stage == "SPEC" else
                      "汇总规划、提交正式任务计划，然后继续派发；状态回复不能替代提交。",
                      tool="loop_stage_request", arguments=team, proceed=True,
                      submission_tool="loop_stage_submit" if stage == "SPEC" else "loop_plan_submit")
    if status != "ACTIVE":
        return action("awaiting_input", state["reason"] or "等待实际输入或权限。")
    if state.get("rework_pending"):
        pending_rework = state["rework_pending"]
        return action("recover_rework", "恢复原返工日志，保留原任务、累计预算和失败证据。",
                      tool="loop_rework", arguments={**team, **{k: pending_rework[k] for k in
                          ("task_id", "check_id", "evidence_digest")}}, proceed=True)
    if state.get("refresh_pending"):
        journal = state["refresh_pending"]
        return action("recover_draft_refresh", "恢复原副本刷新日志，保留原子任务、期限和累计预算。",
                      tool="loop_workbench_refresh", journal=journal, proceed=True,
                      arguments={**team, "task_id": journal["task_id"], "workbench_id": journal["original"],
                                 "reviewed_changes": journal["reviewed_changes"]})
    for operation in state.get("operations", []):
        if operation["status"] == "RUNNING":
            recover = operation["recovery_required"]
            return action("recover_operation" if recover else "wait_operation",
                          "恢复中断的原操作，核对已有副作用并保留累计预算。" if recover else
                          "原检查仍在后台运行；有界等待并报告实际进度，完成后继续修复、评审和交接。",
                          tool="loop_operation_recover" if recover else "loop_operation_status",
                          arguments={**team, "operation_id": operation["id"], **({} if recover else {"wait_seconds": 5})},
                          proceed=True, operation=operation)
    for task_id, record in state["tasks"].items():
        unresolved = [identity for identity, item in record.get("executors", {}).items() if item.get("status") == "RUNNING"]
        if record.get("pending_process") or record.get("pending_edit") or unresolved or record.get("review_preparation"):
            return action("pending_effect", "已有评估或检查进程未完成或需要核对；先处理原操作，不能重复派发。",
                          task_id=task_id, unresolved_attempts=unresolved,
                          review_preparation=record.get("review_preparation"))
    for session in reversed(state.get("verification_sessions", [])):
        if session["unresolved_attempt"]:
            return action("inspect_verification", "核对原验证动作及其实际结果；重新连接或重复调用不代表可以重放动作。",
                          tool="loop_verification_progress", arguments={**team, "session_id": session["id"]},
                          proceed=True, session=session, replay_allowed=False)
    for item in state.get("pending_evaluations", []):
        if item.get("import_pending"):
            return action("import_evaluation", "恢复原操作已生成的签名结果；实际导入和原检查仍需验证，不启动新的评估模型。",
                          tool="loop_evaluation_import", arguments={**team, "task_id": item["task_id"],
                              "envelope": item["cached_envelope"]}, proceed=True, cached_result=item["cached_result"],
                          cached_summary=item["cached_summary"], replay_only=True)
    if plan_configuration.get("problems"):
        repairable = plan_configuration["repairable"]
        return action("repair_plan" if repairable else "invalid_plan_configuration",
                      "未启动的分阶段任务缺少内部阶段图；按默认阶段补齐，保留原契约和已交付成果。" if repairable else
                      "任务内部阶段配置无效；需核对实际配置，不能派发失败任务或声明通过。",
                      tool="loop_repair_plan" if repairable else None, arguments=team if repairable else None,
                      proceed=repairable, problems=plan_configuration["problems"])
    for task_id, record in state["tasks"].items():
        if record["status"] == "HANDOFF":
            missing = [role for role in data["tasks"][task_id]["handoff_to"] if role not in record["receipts"]]
            if missing:
                return action("receive_handoff", "由指定接收角色检查实际输出和检查结果，再记录交接。",
                              tool="loop_receive_handoff", arguments={**team, "task_id": task_id,
                                  "handoff_id": record["handoff"]["id"], "role": missing[0]}, proceed=True)
    pending_evaluations = state.get("pending_evaluations", [])
    failed = next((item for item in pending_evaluations if item.get("result") == "fail"), None)
    if failed:
        if failed.get("native_rework", {}).get("eligible"):
            return action("rework_dependency", "将当前独立评审问题退回已授权修复任务，重新验证受影响的后续任务。",
                          tool="loop_rework", arguments={**team, **{k: failed[k] for k in
                              ("task_id", "check_id", "evidence_digest")}}, proceed=True,
                          recovery=failed["native_rework"])
        record = state["tasks"].get(failed["repair_task"], {})
        prepare_repair = (failed["repair_task"] == failed["task_id"] and not failed["repair_requires_reviewed_plan"] and
                          failed.get("repair_edit_authorized", False) and bool(failed["repair_write_allow"]) and
                          record.get("status") == "RUNNING" and record.get("controller_status") == "PLANNING" and
                          (not record.get("request") or record.get("host_wait", {}).get("status") != "EXPIRED"))
        return action("review_failed", failed["evaluation_summary"] +
                      " 将实际问题交给修复责任人，按允许范围修复并验证新候选；不能重复请求同候选评审来消除失败。",
                      tool="loop_workbench_prepare" if prepare_repair else None,
                      arguments={**team, "task_id": failed["repair_task"]} if prepare_repair else None,
                      proceed=prepare_repair,
                      task_id=failed["task_id"], check_id=failed["check_id"],
                      **{name: failed[name] for name in ("repair_task", "repair_owner", "repair_write_allow",
                          "repair_write_deny", "repair_requires_reviewed_plan")},
                      repair_edit_authorized=failed.get("repair_edit_authorized", False),
                      findings=failed["findings"], evidence_digest=failed["evidence_digest"],
                      recovery=failed.get("native_rework"))
    inconclusive = next((item for item in pending_evaluations if item.get("result") == "inconclusive"), None)
    if inconclusive:
        return action("await_evaluator_input", inconclusive["evaluation_summary"] +
                      " 补齐实际材料、运行条件或评估能力后再评审；同候选的待确认结果不能被空轮询变成通过。",
                      task_id=inconclusive["task_id"], check_id=inconclusive["check_id"], findings=inconclusive["findings"],
                      evidence_digest=inconclusive["evidence_digest"])
    for item in sorted(pending_evaluations, key=lambda item: not item["executor_registered"]):
        registered = item["executor_registered"]
        if item.get("route") == "missing_authority":
            return action("missing_evaluator_authority", item.get("summary") or
                          "当前正式检查缺少有效评估权威；需要实际登记，不能用专业报告声明验收通过。",
                          task_id=item["task_id"], check_id=item["check_id"], check_type=item["type"])
        return action("run_evaluation" if registered else "await_evaluator",
                      "运行已注册的实际检查执行器。" if registered else "缺少独立评估结果；请求签名证据，不能用聊天结论代替。",
                      tool="loop_evaluation_run" if registered else "loop_evaluation_request",
                      arguments={**team, "task_id": item["task_id"], "check_id": item["check_id"]},
                      proceed=registered)
    if data["plan"].get("native_parallel") and state["ready_tasks"] and any(
            r["status"] == "RUNNING" for r in state["tasks"].values()):
        return action("prepare_task", "继续派发依赖已满足且文件归属独立的任务，各自副本开发、逐项整合验证。",
                      tool="loop_workbench_prepare", arguments={**team, "task_id": state["ready_tasks"][0]}, proceed=True)
    for task_id, record in state["tasks"].items():
        if record["status"] == "REJECTED":
            return action("review_rejection", record["reason"] or "交接已退回，需要评审返工范围。")
        if record["status"] != "RUNNING":
            continue
        stale = next((w for w in state.get("worker_drafts", []) if w["task_id"] == task_id and
                      w.get("current") and w.get("refresh_required")), None)
        if stale:
            return action("inspect_draft_base", "其他交付已改变主项目。核对这些实际改动后刷新原副本，保留草稿并重新运行原检查。",
                          tool="loop_workbench_status", arguments={**team, "task_id": task_id}, proceed=True,
                          changed_paths=stale["project_changed_paths"], workbench_id=stale["id"],
                          continuation_tool="loop_workbench_refresh")
        if record.get("controller_created") is False:
            return action("prepare_task", "继续初始化原先保留编号的子任务；初始化失败没有建立子运行，不生成新任务或重置已有预算。",
                          tool="loop_workbench_prepare", arguments={**team, "task_id": task_id}, proceed=True)
        if record.get("pending_process"):
            return action("pending_effect", "已有检查进程未完成或需要恢复，先处理原操作。")
        sessions = [s for s in state.get("verification_sessions", []) if s["task_id"] == task_id and s["current"]]
        if sessions and sessions[-1]["observed_pass"] < sessions[-1]["total"]:
            remaining = [s for s in sessions[-1]["scenarios"] if s["status"] != "OBSERVED_PASS"]
            if not any(s["dependencies_ready"] and s["attempts_remaining"] for s in remaining):
                return action("verification_limit", "验证尝试已用尽，保留结果并报告需要调整的实际边界；不能通过新会话重置次数。",
                              session=sessions[-1])
            return action("continue_verification", "继续当前候选的逐项验证，先登记动作，执行后保存实际观察和附件。",
                          tool="loop_verification_progress", arguments={**team, "session_id": sessions[-1]["id"]},
                          proceed=True, session=sessions[-1], replay_allowed=False)
        workers = [w for w in state.get("dispatch_board", {}).get("workers", []) if w["task_id"] == task_id]
        for worker in workers:
            if worker["current"] and (worker["status"] == "DRAFT_READY" or worker.get("import_pending") or
                                      worker["status"] == "STOPPED" and worker.get("changed_file_count")):
                return action("collect_worker", "检查已返回的专业草稿并立即串行收集，随后补充依赖已满足的工作；专业返回不代表正式检查通过。",
                              tool="loop_worker_collect", arguments={**team, "worker_id": worker["id"]}, proceed=True)
        occupied = [w for w in workers if w["status"] in {"PREPARED", "RUNNING", "DRAFT_READY", "BLOCKED"}]
        if occupied:
            recovery = any(w["expired"] or not w["current"] or w["status"] == "BLOCKED" or w["problem"] for w in occupied)
            return action("recover_worker" if recovery else "coordinate_workers",
                          "处理实际超时、阻塞或旧工作者：由宿主停止其写入并保留草稿，随后收集或有界接手；轮询不能续期。" if recovery else
                          "主线程协调专业分工：检查实际结果、接口和文件归属，在冻结限制与宿主可用槽位内补充就绪工作，及时回收返回结果。",
                          tool="loop_dispatch_board", arguments=team, proceed=True)
        if record.get("request") and record.get("host_wait", {}).get("status") == "EXPIRED":
            return action("recover_worker", "宿主提案等待超时；检查工作副本的实际文件，停止空等。由当前协调者接手，或停止旧工作者后在同一子运行内重新派发；不重置任务预算。",
                          tool="loop_workbench_status", arguments={**team, "task_id": task_id}, proceed=True)
        child = record.get("controller_status")
        if record.get("request") or child == "PLANNING":
            return action("prepare_task", "将绑定的任务上下文交给实际工作者，提交提案并检查结果。",
                          tool="loop_workbench_prepare", arguments={**team, "task_id": task_id}, proceed=True)
        if child == "SUCCEEDED":
            return action("collect_task", "收集当前已验证的结果并准备交接。",
                          tool="loop_collect_task", arguments={**team, "task_id": task_id}, proceed=True)
        return action("task_blocked", record.get("controller_reason") or record.get("reason") or "任务尚未通过检查，需要检查实际子运行状态。",
                      task_id=task_id, controller_status=child, evaluation_outcomes=record.get("evaluation_outcomes", {}))
    if state["ready_tasks"]:
        return action("prepare_task", "派发下一个依赖已满足的正式任务，在独立副本中产生实际代码。", tool="loop_workbench_prepare",
                      arguments={**team, "task_id": state["ready_tasks"][0]}, proceed=True)
    return action("no_ready_task", "没有可派发任务，检查依赖、交接与最终验收状态。")
